#!/bin/bash
# openclaw-tool.sh — Wrapper for Sovereign Memory agent_api.py
# Used by OpenClaw agents (forge, syntra, recon, pulse) to call
# sovereign_recall and sovereign_learn without shell-out complexity.
#
# Usage from OpenClaw agent (via exec):
#   ~/.openclaw/sovereign-memory-v3.1/openclaw-tool.sh recall <agent_id> <query> [limit]
#   ~/.openclaw/sovereign-memory-v3.1/openclaw-tool.sh learn <agent_id> [category] <content>

SOVEREIGN_DIR="$HOME/.openclaw/sovereign-memory-v3.1"
PYTHON="${SOVEREIGN_DIR}/venv/bin/python"
API="${SOVEREIGN_DIR}/agent_api.py"

if [ ! -f "$PYTHON" ] || [ ! -f "$API" ]; then
    echo '{"success": false, "error": "Sovereign Memory engine not found"}'
    exit 1
fi

case "$1" in
    recall)
        # usage: recall <agent_id> <query> [limit]
        AGENT_ID="$2"
        QUERY="$3"
        LIMIT="${4:-5}"

        if [ -z "$AGENT_ID" ] || [ -z "$QUERY" ]; then
            echo '{"success": false, "error": "agent_id and query are required"}'
            exit 1
        fi

        # Run recall, filter MLX/sentence-transformer noise from stderr
        STDOUT=$("$PYTHON" "$API" "$AGENT_ID" "$QUERY" 2>/dev/null)
        EXIT_CODE=$?

        if [ $EXIT_CODE -ne 0 ]; then
            echo '{"success": false, "error": "Sovereign Memory recall failed"}'
            exit 1
        fi

        if [ -z "$STDOUT" ]; then
            echo '{"success": true, "result": "No results found."}'
        else
            # Escape double quotes and collapse whitespace for JSON safety
            SAFE=$(echo "$STDOUT" | sed 's/\\/\\\\/g' | sed 's/"/\\"/g' | tr '\n' ' ' | sed 's/  */ /g' | sed 's/ *$//')
            echo "{\"success\": true, \"result\": \"${SAFE}\"}"
        fi
        ;;

    learn)
        # usage: learn <agent_id> [category] <content>
        AGENT_ID="$2"
        shift 2

        if [ -z "$AGENT_ID" ]; then
            echo '{"success": false, "error": "agent_id is required"}'
            exit 1
        fi

        ALL_ARGS="$*"
        if [ -z "$ALL_ARGS" ]; then
            echo '{"success": false, "error": "content is required"}'
            exit 1
        fi

        # Determine category vs content
        FIRST_WORD=$(echo "$ALL_ARGS" | awk '{print $1}')
        case "$FIRST_WORD" in
            fact|event|decision|preference|learning|fix|pattern|general)
                CATEGORY="$FIRST_WORD"
                CONTENT=$(echo "$ALL_ARGS" | cut -d' ' -f2-)
                ;;
            *)
                CATEGORY="general"
                CONTENT="$ALL_ARGS"
                ;;
        esac

        # Run learn: python agent_api.py <agent_id> --learn "[category] content"
        LEARN_TEXT="[${CATEGORY}] ${CONTENT}"
        STDOUT=$("$PYTHON" "$API" "$AGENT_ID" --learn "$LEARN_TEXT" 2>/dev/null)
        EXIT_CODE=$?

        if [ $EXIT_CODE -ne 0 ]; then
            echo '{"success": false, "error": "Sovereign Memory learn failed"}'
            exit 1
        fi

        SAFE=$(echo "$STDOUT" | sed 's/\\/\\\\/g' | sed 's/"/\\"/g')
        echo "{\"success\": true, \"result\": \"${SAFE}\"}"
        ;;

    *)
        echo '{"success": false, "error": "Unknown command. Use: recall|learn"}'
        exit 1
        ;;
esac
