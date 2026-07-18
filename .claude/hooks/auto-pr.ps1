# Auto-PR hook (Stop event). See settings.local.json for how this is wired up.
#
# On a dirty tree: opens a PR for whatever changed, drafting the description
# and requesting a code review via a local Ollama model (free, no API calls),
# or, if already on an open auto-PR branch, just pushes more commits to it.
# The review only runs once, at PR creation, not on every push. None of this
# ever prompts for confirmation.

$ollamaModel = if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { 'qwen2.5-coder:7b' }
$ollamaHost = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { 'http://localhost:11434' }

function Emit($msg) {
    (@{ systemMessage = $msg } | ConvertTo-Json -Compress)
}

$statusLines = git status --porcelain
if (-not $statusLines) {
    exit 0
}

$branch = (git rev-parse --abbrev-ref HEAD).Trim()

if ($branch -ne 'master' -and $branch -notlike 'claude/auto-*') {
    exit 0
}

function Test-OllamaAvailable {
    # Cheap reachability check so we can warn once up front instead of letting
    # every Invoke-Ollama call fail silently and time out one by one (see issue #6).
    try {
        Invoke-RestMethod -Uri "$ollamaHost/api/tags" -Method Get -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Invoke-Ollama($systemPrompt, $userPrompt, $schema) {
    $payload = @{
        model    = $ollamaModel
        messages = @(
            @{ role = 'system'; content = $systemPrompt }
            @{ role = 'user'; content = $userPrompt }
        )
        stream   = $false
    }
    # A bare "json" format forces valid JSON syntax but, on a 7B model, tends to
    # suppress the reasoning that finds real issues -- callers that need
    # structured output should do a free-form pass first and structure it in a
    # second call (see Invoke-CodeReview), passing the schema only there.
    if ($schema) { $payload['format'] = $schema }
    try {
        $resp = Invoke-RestMethod -Uri "$ollamaHost/api/chat" -Method Post `
            -Body ($payload | ConvertTo-Json -Depth 10 -Compress) -ContentType 'application/json' `
            -TimeoutSec 180
        return $resp.message.content
    } catch {
        return $null
    }
}

$script:findingSchema = @{
    type  = 'array'
    items = @{
        type       = 'object'
        properties = @{
            line              = @{ type = 'integer' }
            summary           = @{ type = 'string' }
            failure_scenario  = @{ type = 'string' }
        }
        required   = @('line', 'summary', 'failure_scenario')
    }
}

function New-PrBody($diff) {
    try {
        $maxChars = 12000
        $truncated = $diff.Length -gt $maxChars
        if ($truncated) {
            $diff = $diff.Substring(0, $maxChars)
        }
        $note = if ($truncated) { ' (truncated)' } else { '' }
        $system = 'You draft concise, accurate GitHub pull request descriptions from diffs. Output only the Markdown body -- no title, no text outside the requested sections.'
        $user = @"
Draft a PR description for the diff below. Use exactly these three sections, in this order:

## Purpose
What this change does and why.

## Risk
What could break, the blast radius, and how to roll back if needed.

## Issues Addressed
The concrete problem or gap this solves. If none is evident from the diff, say so plainly.

Diff${note}:
$diff
"@
        $text = Invoke-Ollama -systemPrompt $system -userPrompt $user
        if (-not $text) { return $null }
        $text = $text.Trim()
        if (-not $text) { return $null }
        return $text
    } catch {
        return $null
    }
}

function Get-CommentableLines($diffText) {
    # Maps out which new-file line numbers actually appear in the diff, since
    # GitHub's inline PR comment API rejects lines outside it.
    $result = @{}
    $newLineNum = 0
    foreach ($line in ($diffText -split "`n")) {
        if ($line -match '^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@') {
            $newLineNum = [int]$matches[1]
            continue
        }
        if ($line.StartsWith('+++') -or $line.StartsWith('---')) { continue }
        if ($line.StartsWith('+') -or $line.StartsWith(' ')) {
            $result[$newLineNum] = $true
            $newLineNum++
        }
        # '-' lines are deletions: absent from the new file, so they don't
        # advance the new-line counter and can't be commented on via 'side=RIGHT'.
    }
    return $result
}

function Invoke-CodeReview($prNumber, $diffBase, $headSha, $repoSlug) {
    $maxCharsPerFile = 12000
    $postedCount = 0
    $files = @(git diff --name-only "$diffBase...HEAD")

    foreach ($file in $files) {
        $fileDiff = (git diff "$diffBase...HEAD" -- $file | Out-String)
        if (-not $fileDiff.Trim()) { continue }

        $commentable = Get-CommentableLines $fileDiff
        if ($commentable.Count -eq 0) { continue }

        $truncated = $fileDiff.Length -gt $maxCharsPerFile
        if ($truncated) { $fileDiff = $fileDiff.Substring(0, $maxCharsPerFile) }
        $fileNote = if ($truncated) { ' (diff truncated)' } else { '' }

        # Stage 1: free-form analysis. Unconstrained generation finds real
        # issues; a JSON-schema-constrained first pass on a 7B model tends to
        # come back empty even on obviously buggy diffs (verified experimentally).
        $analysisSystem = 'You are a rigorous senior code reviewer holding this change to a high standard of clean, effective, maintainable code. Look hard for concrete correctness bugs: missing validation, race conditions, unchecked preconditions, error handling gaps, resource leaks. Do not invent issues that are not there.'
        $analysisUser = "File: $file$fileNote`n`nDiff:`n$fileDiff`n`nList concrete issues, plain text, each tied to a specific new-file line number from the diff. If there are none, say so."
        $analysis = Invoke-Ollama -systemPrompt $analysisSystem -userPrompt $analysisUser
        if (-not $analysis) { continue }

        # Stage 2: structure the stage-1 analysis into the required JSON shape.
        $structureSystem = 'Convert a code review analysis into a strict JSON array. Each element: {"line": <int, a new-file line number from the diff>, "summary": <short string>, "failure_scenario": <string>}. Only include items that reference a concrete line number. If the analysis has no findings, return [].'
        $structureUser = "Diff:`n$fileDiff`n`nAnalysis to convert:`n$analysis"
        $raw = Invoke-Ollama -systemPrompt $structureSystem -userPrompt $structureUser -schema $script:findingSchema
        if (-not $raw) { continue }

        try {
            $findings = @($raw | ConvertFrom-Json)
        } catch {
            continue
        }

        foreach ($finding in $findings) {
            if (-not $finding.line) { continue }
            $lineNum = [int]$finding.line
            if (-not $commentable.ContainsKey($lineNum)) { continue }
            if (-not $finding.summary) { continue }

            $body = "**$($finding.summary)**`n`n$($finding.failure_scenario)`n`n_Posted by the free local code-review agent ($ollamaModel via Ollama)._"
            gh api "repos/$repoSlug/pulls/$prNumber/comments" `
                -f "commit_id=$headSha" -f "path=$file" -F "line=$lineNum" -f "side=RIGHT" -f "body=$body" *>$null
            if ($LASTEXITCODE -eq 0) { $postedCount++ }
        }
    }

    return $postedCount
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
        # Check once, up front, rather than letting the description draft and
        # every per-file review call each independently fail and time out.
        $ollamaAvailable = Test-OllamaAvailable
        $ollamaNote = ''
        if (-not $ollamaAvailable) {
            $ollamaNote = " Ollama ($ollamaHost) was unreachable, so the PR got a generic description and no automated code review ran. Start Ollama (ollama serve) with the $ollamaModel model pulled to enable both."
            Write-Output "Warning: Ollama ($ollamaHost) is unreachable -- skipping PR description drafting and code review for this PR."
        }

        $diff = (git diff "$diffBase...HEAD" | Out-String)
        $body = if ($ollamaAvailable) { New-PrBody $diff } else { $null }
        if (-not $body) {
            $body = "Automated PR opened by a Claude Code hook after changes were made in this repo.`n`nReview and merge (or close) as appropriate."
        }
        gh pr create --title "Automated changes ($branch)" --body $body --head $branch --base $baseRef *>$null
        if ($LASTEXITCODE -ne 0) { throw "gh pr create failed for $branch (branch was pushed)" }

        $prNumber = gh pr view $branch --json number -q .number 2>$null
        $headSha = (git rev-parse HEAD).Trim()
        $repoSlug = gh repo view --json nameWithOwner -q .nameWithOwner 2>$null
        $reviewCount = 0
        if ($ollamaAvailable -and $prNumber -and $repoSlug) {
            $reviewCount = Invoke-CodeReview -prNumber $prNumber -diffBase $diffBase -headSha $headSha -repoSlug $repoSlug
        }
        Write-Output (Emit "Auto-PR hook: opened branch $branch, drafted a PR description, and posted $reviewCount free local code-review comment(s).$ollamaNote")
    } else {
        Write-Output (Emit "Auto-PR hook: pushed more changes to $branch.")
    }
}
catch {
    Write-Output (Emit "Auto-PR hook failed: $($_.Exception.Message)")
}
