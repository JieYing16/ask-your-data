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

try {
    $needsNewBranch = ($branch -eq 'master')

    if (-not $needsNewBranch) {
        # Only keep pushing to this branch if it still has an open PR; otherwise cut a fresh one.
        $prState = gh pr view $branch --json state -q .state 2>$null
        if ($LASTEXITCODE -ne 0 -or $prState -ne 'OPEN') {
            git checkout master *>$null
            if ($LASTEXITCODE -ne 0) { throw "could not switch back to master to cut a fresh branch" }
            $needsNewBranch = $true
        }
    }

    if ($needsNewBranch) {
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
        $body = "Automated PR opened by a Claude Code hook after changes were made in this repo.`n`nReview and merge (or close) as appropriate."
        gh pr create --title "Automated changes ($branch)" --body $body --head $branch --base master *>$null
        if ($LASTEXITCODE -ne 0) { throw "gh pr create failed for $branch (branch was pushed)" }
        Write-Output (Emit "Auto-PR hook: opened branch $branch and created a PR.")
    } else {
        Write-Output (Emit "Auto-PR hook: pushed more changes to $branch.")
    }
}
catch {
    Write-Output (Emit "Auto-PR hook failed: $($_.Exception.Message)")
}
