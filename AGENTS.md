# Fast File Upload Agent Notes

## Agentic Workflows

After modifying any `.md` workflow file under `.github/workflows/`, recompile and commit the generated workflow files together with the source change when GitHub Agentic Workflows is available:

```bash
gh aw compile
apm compile
```

For Goal issues, keep the completion contract evidence-based. A goal is complete only when the issue's stated verification evidence supports it.

## Local Goal Fallback

When GitHub CLI authentication is unavailable, maintain the current goal contract and iteration log inside `.monkeycode/docs/` so the repository still keeps durable progress state until remote Goal automation is enabled.
