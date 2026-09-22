from datetime import datetime, timedelta
from utils.time import utcnow_naive
from pathlib import Path
import shutil

from sqlalchemy.orm import Session

from control_plane.config import CP_RETENTION_DAYS, CP_DATABASE_URL
from control_plane.models import IngestEvent, MetricPoint, AuditLog


def cleanup_retention(db: Session) -> dict:
    cutoff = utcnow_naive() - timedelta(days=CP_RETENTION_DAYS)
    del_events = db.query(IngestEvent).filter(IngestEvent.created_at < cutoff).delete(synchronize_session=False)
    del_metrics = db.query(MetricPoint).filter(MetricPoint.created_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return {"deleted_events": int(del_events or 0), "deleted_metrics": int(del_metrics or 0)}


def make_backup() -> str:
    # Supports sqlite URL: sqlite:///path/to/file.db
    if not CP_DATABASE_URL.startswith("sqlite:///"):
        return ""
    src = Path(CP_DATABASE_URL.replace("sqlite:///", ""))
    if not src.exists():
        return ""
    dst_dir = Path("data/control_plane_backups")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"control_plane_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(src, dst)
    return str(dst)
