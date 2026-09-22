# Troubleshooting

| Symptom | Check | Action |
|---|---|---|
| live works, ready is 503 | logs and both SQLite paths | Correct ownership/paths; do not delete databases. |
| Parser tasks run twice | embedded mode and parser units | Keep exactly one parser mode. |
| database is locked | duplicate processes and long writes | Stop duplicate parser/bot instances; retain WAL and busy timeout. |
| Panel unavailable remotely | expected loopback bind | Use SSH tunnel or nginx HTTPS; do not open 8081. |
| Bot cannot start | Telegram env keys | Fix the env file and keep mode 600. |
| Update failed | backup and staged source | Restore persistent files; do not run first-install over the app. |
| Update exit 3 (rolled back) | readiness/version gate failed | Previous code + pre-update backup already restored; inspect logs, fix cause, retry with a pinned SHA. |
| Update exit 4 (rollback incomplete) | post-rollback health still failing | Manual recovery per RUNBOOK 17: stop services, re-apply previous SHA, extract `.env`/`data/` from the printed backup archive, restart cp then bot. |
| `/version` SHA != deployed SHA | new bot + old Control Plane state | Never leave this state: re-run the update (it re-syncs both services) or roll back; both units must be active on one SHA. |
| `release_status.sh` shows MISMATCH | manifest vs live drift | Someone changed code without the update flow; redeploy the pinned SHA. |

Useful commands:

```bash
systemctl status corebot.service corebot-cp.service --no-pager -l
journalctl -u corebot.service -u corebot-cp.service -n 200 --no-pager
curl -i http://127.0.0.1:8081/health/ready
ss -ltnp | grep 8081
```
