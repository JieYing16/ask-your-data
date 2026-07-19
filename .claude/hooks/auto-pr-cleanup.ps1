# Recovery tool for auto-pr.ps1 (see auto-pr.ps1 and issue #4).
#
# auto-pr.ps1 pushes a claude/auto-* branch and then calls `gh pr create`. If
# the push succeeds but PR creation fails (auth glitch, rate-limit, network),
# the branch is left on the remote with no PR pointing at it -- an orphan.
# This script finds those and, on request, deletes them.
#
# Usage:
#   pwsh .claude/hooks/auto-pr-cleanup.ps1            # list orphans (dry run)
#   pwsh .claude/hooks/auto-pr-cleanup.ps1 -Delete     # delete local + remote

param(
    [switch]$Delete
)

git fetch origin --prune *>$null

$branches = @(git branch -r --format '%(refname:short)') |
    Where-Object { $_ -like 'origin/claude/auto-*' } |
    ForEach-Object { $_ -replace '^origin/', '' }

if (-not $branches) {
    Write-Output 'No claude/auto-* branches found on origin.'
    exit 0
}

$orphans = @()
foreach ($branch in $branches) {
    $prState = gh pr view $branch --json state -q .state 2>$null
    $prCheckFailed = ($LASTEXITCODE -ne 0)
    # No PR at all is a definitive "orphan". A failed gh call is inconclusive
    # (auth/network/rate-limit) -- skip it rather than risk deleting a branch
    # that actually has an open PR.
    if ($prCheckFailed) {
        Write-Output "Skipping $branch -- could not determine PR state (gh call failed)."
        continue
    }
    if (-not $prState) {
        $orphans += $branch
    }
}

if (-not $orphans) {
    Write-Output 'No orphaned branches found.'
    exit 0
}

Write-Output "Orphaned branches (pushed, no PR):"
$orphans | ForEach-Object { Write-Output "  $_" }

if (-not $Delete) {
    Write-Output "`nRun with -Delete to remove these branches locally and on origin."
    exit 0
}

foreach ($branch in $orphans) {
    git push origin --delete $branch *>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Output "Failed to delete origin/$branch"
    } else {
        Write-Output "Deleted origin/$branch"
    }

    $hasLocal = (git branch --list $branch)
    if ($hasLocal) {
        git branch -D $branch *>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Output "Failed to delete local branch $branch"
        } else {
            Write-Output "Deleted local branch $branch"
        }
    }
}
