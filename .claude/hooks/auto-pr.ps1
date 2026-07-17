# Auto-PR hook (Stop event). See settings.local.json for how this is wired up.
#
# On a dirty tree: opens a PR for whatever changed (drafting the description
# from the diff and requesting an automated code review, both via headless
# `claude -p` calls), or, if already on an open auto-PR branch, just pushes
# more commits to it. The review only runs once, at PR creation, not on every
# push. None of this ever prompts for confirmation.

$reviewEffort = 'high'
$maxSpendUsd = 2.00

function Emit($msg) {
    (@{ systemMessage = $msg } | ConvertTo-Json -Compress)
}

# A nested `claude -p` call below is itself a full session and will hit its own
# Stop event. This guard stops that nested run from re-entering this script.
if ($env:CLAUDE_AUTO_PR_HOOK_RUNNING -eq '1') {
    exit 0
}

$statusLines = git status --porcelain
if (-not $statusLines) {
    exit 0
}

$branch = (git rev-parse --abbrev-ref HEAD).Trim()

if ($branch -ne 'master' -and $branch -notlike 'claude/auto-*') {
    exit 0
}

function New-PrBody($diff) {
    try {
        $maxChars = 6000
        $truncated = $diff.Length -gt $maxChars
        if ($truncated) {
            $diff = $diff.Substring(0, $maxChars)
        }
        $note = if ($truncated) { ' (truncated)' } else { '' }
        $prompt = @"
Draft a concise GitHub pull request description in Markdown for the diff below.
Use exactly these three sections, in this order:

## Purpose
What this change does and why.

## Risk
What could break, the blast radius, and how to roll back if needed.

## Issues Addressed
The concrete problem or gap this solves. If none is evident from the diff, say so plainly.

Do not include a title or any text outside these three sections.

Diff${note}:
$diff
"@
        $env:CLAUDE_AUTO_PR_HOOK_RUNNING = '1'
        try {
            # Pipe the prompt over stdin rather than passing it as a positional
            # argument: a diff-sized prompt is big enough to break as a native
            # command-line argument, and piping also avoids inheriting the
            # hook's own stdin pipe (which otherwise makes claude.exe stall for
            # a few seconds probing it).
            $result = $prompt | claude -p --tools "" --output-format text `
                --max-budget-usd 0.50 --no-session-persistence 2>$null
        } finally {
            Remove-Item Env:\CLAUDE_AUTO_PR_HOOK_RUNNING -ErrorAction SilentlyContinue
        }
        if ($LASTEXITCODE -ne 0 -or -not $result) {
            return $null
        }
        $text = ($result -join "`n").Trim()
        if (-not $text) { return $null }
        return $text
    } catch {
        return $null
    }
}

function Invoke-CodeReview {
    try {
        $env:CLAUDE_AUTO_PR_HOOK_RUNNING = '1'
        try {
            $null | claude -p "/code-review $reviewEffort --comment" `
                --append-system-prompt "You are a rigorous senior code reviewer holding this PR to a high standard of clean, effective, maintainable code. Flag anything that falls short." `
                --permission-mode bypassPermissions `
                --output-format text `
                --max-budget-usd $maxSpendUsd `
                --no-session-persistence *>$null
        } finally {
            Remove-Item Env:\CLAUDE_AUTO_PR_HOOK_RUNNING -ErrorAction SilentlyContinue
        }
    } catch {
        # Best-effort: a failed review pass shouldn't be treated as a hook failure.
    }
}

try {
    $needsNewBranch = ($branch -eq 'master')

    if (-not $needsNewBranch) {
        # Only cut a fresh branch on a *definitive* non-open state. A failed gh
        # call (auth/network/rate-limit) is inconclusive, not evidence the PR is
        # closed -- treat it as "still open" and keep pushing here, rather than
        # risk orphaning a real open PR behind a duplicate branch.
        $prState = gh pr view $branch --json state -q .state 2>$null
        $prCheckFailed = ($LASTEXITCODE -ne 0)
        if (-not $prCheckFailed -and $prState -ne 'OPEN') {
            $staleBranch = $branch
            git checkout master *>$null
            if ($LASTEXITCODE -ne 0) { throw "could not switch back to master to cut a fresh branch" }
            git branch -D $staleBranch *>$null
            $needsNewBranch = $true
        }
    }

    $baseRef = 'master'

    if ($needsNewBranch) {
        # Read-only; safe even with a dirty tree. Lets the PR description (and,
        # when nothing local conflicts, the branch point) reflect current master
        # instead of a possibly-stale local copy.
        git fetch origin master *>$null
        $diffBase = if ($LASTEXITCODE -eq 0) { 'origin/master' } else { $baseRef }

        $branch = "claude/auto-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        git checkout -b $branch *>$null
        if ($LASTEXITCODE -ne 0) { throw "could not create branch $branch" }
    }

    git add -A
    git commit -m "Automated commit from Claude Code session" *>$null
    if ($LASTEXITCODE -ne 0) { throw "git commit failed" }

    if ($needsNewBranch) {
        git push -u origin $branch *>$null
    } else {
        git push *>$null
    }
    if ($LASTEXITCODE -ne 0) { throw "git push failed for $branch" }

    if ($needsNewBranch) {
        $diff = (git diff "$diffBase...HEAD" | Out-String)
        $body = New-PrBody $diff
        if (-not $body) {
            $body = "Automated PR opened by a Claude Code hook after changes were made in this repo.`n`nReview and merge (or close) as appropriate."
        }
        gh pr create --title "Automated changes ($branch)" --body $body --head $branch --base $baseRef *>$null
        if ($LASTEXITCODE -ne 0) { throw "gh pr create failed for $branch (branch was pushed)" }
        Invoke-CodeReview
        Write-Output (Emit "Auto-PR hook: opened branch $branch, drafted a PR description, and requested a code review.")
    } else {
        Write-Output (Emit "Auto-PR hook: pushed more changes to $branch.")
    }
}
catch {
    Write-Output (Emit "Auto-PR hook failed: $($_.Exception.Message)")
}
