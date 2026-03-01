#!/bin/bash
# Evening Reflection Trigger
# Sends a macOS notification at 10pm to start your reflection
# 
# To install this cron job, run:
#   crontab -e
# Then add this line:
#   0 22 * * * /Users/nazymzhiyengaliyeva/obsidian-mcp/evening_trigger.sh

osascript -e 'display notification "Time for your evening reflection. Open Claude Desktop and say: run my evening reflection" with title "Obsidian 🌙" sound name "Chime"'
