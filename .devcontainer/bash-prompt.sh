# Loaded by interactive Bash terminals, including login shells.
# Git's prompt helper safely renders the actual checked-out branch.
source /usr/lib/git-core/git-sh-prompt
PS1='\[\e[32m\]${GITHUB_USER:+@}${GITHUB_USER:-\u} ➜ \[\e[34m\]\w\[\e[33m\]$(__git_ps1 " (%s)")\[\e[0m\] \$ '
