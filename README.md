# skills

General-purpose [Claude Code](https://claude.com/claude-code) skills.

| Skill | What it does |
|---|---|
| `analyze-ci` | Poll a PR's CI checks until they finish, diagnose failures, fix the easy ones |
| `claude-new` | Start a background Claude session in a project |
| `last-screenshot` | Pull the most recent Desktop screenshot into the conversation |
| `smart-pr` | Generate or update a PR title and description from the branch diff |
| `sonarcloud-issues` | List SonarCloud/SonarQube issues for a PR |
| `unresolved-comments` | Show a PR's unresolved review comments and work through them |
| `whatsapp-to-db` | Turn a WhatsApp chat export into a searchable SQLite database |
| `yolo` | Commit, push, open a PR, watch CI and merge, end to end |

## Install

As a plugin (skills are namespaced, e.g. `/skills:yolo`):

```
/plugin marketplace add bonzofenix/skills
/plugin install skills@bonzofenix-skills
```

Or symlink them un-namespaced (`/yolo`):

```
git clone https://github.com/bonzofenix/skills ~/workspace/skills
for d in ~/workspace/skills/skills/*/; do ln -sfn "$d" ~/.claude/skills/"$(basename "$d")"; done
```

[`bonzofenix/workstation`](https://github.com/bonzofenix/workstation)'s
`make configurations` does the symlinking for you.
