## Dev environment tips

- **Python version**: Use Python 3.10+ and uv dependencies manager.
- **Virtual env (recommended)**:
```bash
uv sync
```

### Quick run examples
  ```bash
  uv run pyats-mcp
  ```


## PR instructions

- **Security**: Do not commit real credentials or tokens. Use placeholders and document required env vars or files.

## Contribution conventions

- **Backward compatibility**: Do not change existing sample behavior unless clearly improving or fixing a bug; document changes.
