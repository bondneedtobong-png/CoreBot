# Troubleshooting

| Symptom | Check | Action |
|---|---|---|
| live works, ready is 503 | logs and both SQLite paths | Correct ownership/paths; do not delete databases. |
| Parser tasks run twice | embedded mode and parser units | Keep exactly one parser mode. |
| database is locked | duplicate processes and long writes | Stop duplicate parser/bot instances; retain WAL and busy timeout. |
| Panel unavailable remotely | expected loopback bind | Use SSH tunnel or nginx HTTPS; do not open 8081. |
| Bot cannot start | Telegram env keys | Fix the env file and keep mode 600. |
| Update failed | backup and staged source | Restore persistent files; do not run first-install over the app. |

Useful commands:

```bash
systemctl status corebot.service corebot-cp.service --no-pager -l
journalctl -u corebot.service -u corebot-cp.service -n 200 --no-pager
curl -i http://127.0.0.1:8081/health/ready
ss -ltnp | grep 8081
```
