# ai-talks-monitor workspace

This directory is the **source** for the ai-talks-monitor skill.

The **live/deployed** skill is at: `~/.claude/skills/ai-talks-monitor/`

## After any code or config change

Always rsync from here to the installed skill before running it:

```bash
rsync -av \
  --exclude='state.json' \
  --exclude='candidates.json' \
  --exclude='ai_talks.xml' \
  ~/.openclaw/workspace/ai-talks-monitor/ \
  ~/.claude/skills/ai-talks-monitor/
```

Never edit the installed skill directly — changes made there will be lost the next time you rsync from this workspace.