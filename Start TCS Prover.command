#!/bin/zsh
# Run from this file's folder so prompt.txt and the agent are found reliably.
cd "${0:A:h}" || exit 1

# Start TCS Prover; it opens the default browser automatically.
exec python3 web_ui.py
