import os
import io
import csv
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import init_db, get_db, BatteryLog, ChargeEvent, RingDevice, SessionLocal
from app.oura_client import OuraClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oura_battery_tracker")

app = FastAPI(
    title="Oura Ring Battery & Health Monitor",
    description="Containerized app dedicated exclusively to Oura Ring battery tracking, discharge rates, and health diagnostics with multi-ring support and full historical backfill.",
    version="2.4.0"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

oura_client = OuraClient()


@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("Database initialized for Multi-Ring Battery Monitoring with Full Historical Backfilling.")
    asyncio.create_task(background_battery_poller())


async def backfill_ring_history(ring: RingDevice, db: Session, days_back: int = 365) -> int:
    """Fetch and backfill full battery history from Oura API since setup date into SQLite."""
    try:
        live = await oura_client.get_ring_battery(token_override=ring.pat_token, days_back=days_back)
        items = live.get("data", [])
        if not items:
            return 0

        # Sort items chronologically
        items.sort(key=lambda x: str(x.get("timestamp", "")))
        inserted = 0
        charge_start_level = None
        charge_start_time = None
        charge_peak_level = None
        charge_peak_time = None
        prev_level = None
        prev_time = None

        for item in items:
            lvl = item.get("battery_level", 0)
            status = item.get("battery_status", "normal")
            fw = item.get("firmware_version")
            hw = item.get("hardware_type")
            set_up_at = item.get("set_up_at", "")
            ts_str = item.get("timestamp")

            if set_up_at and not ring.purchase_date:
                try:
                    dt_setup = datetime.fromisoformat(set_up_at.replace("Z", "+00:00"))
                    ring.purchase_date = dt_setup.strftime("%Y-%m-%d")
                except Exception:
                    pass

            if not lvl or not ts_str:
                continue

            try:
                dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue

            # Check if log already exists near this timestamp (within 10 mins)
            existing = db.query(BatteryLog).filter(
                BatteryLog.ring_id == ring.id,
                BatteryLog.timestamp >= dt - timedelta(minutes=10),
                BatteryLog.timestamp <= dt + timedelta(minutes=10)
            ).first()

            if not existing:
                log_entry = BatteryLog(
                    ring_id=ring.id,
                    battery_level=lvl,
                    battery_status=status,
                    is_charging=(status in ["charging", "full"]),
                    firmware_version=fw or ring.firmware_version,
                    hardware_type=hw or ring.hardware_type,
                    timestamp=dt
                )
                db.add(log_entry)
                inserted += 1

            # Detect historical charge event (robust peak tracking)
            if prev_level is not None:
                if lvl > prev_level:
                    if charge_start_level is None:
                        charge_start_level = prev_level
                        charge_start_time = prev_time
                        charge_peak_level = lvl
                        charge_peak_time = dt
                    else:
                        if lvl >= charge_peak_level:
                            charge_peak_level = lvl
                            charge_peak_time = dt
                elif charge_start_level is not None:
                    # Battery is flat or dropping.
                    # If we haven't seen a new peak in over 45 minutes, the charging session is over.
                    if charge_peak_time and (dt - charge_peak_time).total_seconds() > 2700:
                        if charge_peak_level > charge_start_level + 8:
                            existing_charge = db.query(ChargeEvent).filter(
                                ChargeEvent.ring_id == ring.id,
                                ChargeEvent.start_time >= charge_start_time - timedelta(hours=1),
                                ChargeEvent.start_time <= charge_start_time + timedelta(hours=1)
                            ).first()
                            
                            if not existing_charge:
                                event = ChargeEvent(
                                    ring_id=ring.id,
                                    start_time=charge_start_time,
                                    end_time=charge_peak_time,
                                    start_level=charge_start_level,
                                    end_level=charge_peak_level,
                                    duration_minutes=max(1, int((charge_peak_time - charge_start_time).total_seconds() / 60))
                                )
                                db.add(event)
                        
                        # Reset tracking
                        charge_start_level = None
                        charge_start_time = None
                        charge_peak_level = None
                        charge_peak_time = None

            prev_level = lvl
            prev_time = dt
            
        # Flush any ongoing charge at the end of the history
        if charge_start_level is not None and charge_peak_level is not None:
            if charge_peak_level > charge_start_level + 8:
                existing_charge = db.query(ChargeEvent).filter(
                    ChargeEvent.ring_id == ring.id,
                    ChargeEvent.start_time >= charge_start_time - timedelta(hours=1),
                    ChargeEvent.start_time <= charge_start_time + timedelta(hours=1)
                ).first()
                if not existing_charge:
                    event = ChargeEvent(
                        ring_id=ring.id,
                        start_time=charge_start_time,
                        end_time=charge_peak_time,
                        start_level=charge_start_level,
                        end_level=charge_peak_level,
                        duration_minutes=max(1, int((charge_peak_time - charge_start_time).total_seconds() / 60))
                    )
                    db.add(event)

        if inserted > 0:
            db.commit()
            logger.info(f"Backfilled {inserted} historical battery records for ring '{ring.label}'")
        return inserted
    except Exception as e:
        logger.warning(f"Error backfilling history for ring '{ring.label}': {e}")
        return 0


async def background_battery_poller():
    """Background task to record battery level periodically for all registered active rings."""
    poll_interval = int(os.getenv("POLL_INTERVAL_MINUTES", "30")) * 60
    while True:
        try:
            db = SessionLocal()
            try:
                rings = db.query(RingDevice).filter(RingDevice.is_active == True).all()
                for ring in rings:
                    try:
                        count = db.query(BatteryLog).filter(BatteryLog.ring_id == ring.id).count()
                        if count < 5:
                            await backfill_ring_history(ring, db, days_back=365)

                        data = await oura_client.get_ring_battery(token_override=ring.pat_token, days_back=7)
                        items = data.get("data", [])
                        if items:
                            latest = items[-1] if isinstance(items, list) else items
                            level = latest.get("battery_level", 0)
                            status = latest.get("battery_status", "normal")
                            is_charging = (status == "charging") or (status == "full")
                            hw = latest.get("hardware_type")
                            fw = latest.get("firmware_version")
                            set_up_at = latest.get("set_up_at", "")

                            if hw and ring.hardware_type != hw:
                                ring.hardware_type = hw
                            if fw and ring.firmware_version != fw:
                                ring.firmware_version = fw
                            
                            # Auto-detect purchase/setup date if empty
                            if set_up_at and not ring.purchase_date:
                                try:
                                    dt_setup = datetime.fromisoformat(set_up_at.replace("Z", "+00:00"))
                                    ring.purchase_date = dt_setup.strftime("%Y-%m-%d")
                                except Exception:
                                    pass

                            prev_log = db.query(BatteryLog).filter(BatteryLog.ring_id == ring.id).order_by(BatteryLog.timestamp.desc()).first()
                            if prev_log and level > prev_log.battery_level + 10:
                                event = ChargeEvent(
                                    ring_id=ring.id,
                                    start_time=prev_log.timestamp,
                                    end_time=datetime.utcnow(),
                                    start_level=prev_log.battery_level,
                                    end_level=level,
                                    duration_minutes=int((datetime.utcnow() - prev_log.timestamp).total_seconds() / 60)
                                )
                                db.add(event)

                            log_entry = BatteryLog(
                                ring_id=ring.id,
                                battery_level=level,
                                battery_status=status,
                                is_charging=is_charging,
                                firmware_version=fw,
                                hardware_type=hw
                            )
                            db.add(log_entry)
                            db.commit()
                            logger.info(f"Recorded battery log for {ring.label} (ID: {ring.id}): {level}% ({status})")
                    except Exception as ring_err:
                        logger.error(f"Error polling battery for ring '{ring.label}': {ring_err}")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error in multi-ring background battery poller: {e}")
        
        await asyncio.sleep(poll_interval)


@app.get("/", response_class=HTMLResponse)
async def dashboard_view(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/api/status")
async def app_status(db: Session = Depends(get_db)):
    rings_count = db.query(RingDevice).filter(RingDevice.is_active == True).count()
    return {
        "status": "online",
        "pat_configured": rings_count > 0,
        "ring_count": rings_count,
        "poll_interval_minutes": int(os.getenv("POLL_INTERVAL_MINUTES", "30")),
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }


@app.get("/api/rings")
async def get_rings(db: Session = Depends(get_db)):
    """Fetch all configured active rings with customization details."""
    rings = db.query(RingDevice).filter(RingDevice.is_active == True).all()
    result = []
    for r in rings:
        latest_log = db.query(BatteryLog).filter(BatteryLog.ring_id == r.id, BatteryLog.battery_level > 0).order_by(BatteryLog.timestamp.desc()).first()
        result.append({
            "id": r.id,
            "label": r.label,
            "emoji": r.emoji or "💍",
            "color_tag": r.color_tag or "teal",
            "purchase_date": r.purchase_date or "",
            "target_days": r.target_days or 7.0,
            "hardware_type": r.hardware_type or "Oura Ring",
            "firmware_version": r.firmware_version or "Latest",
            "battery_level": latest_log.battery_level if latest_log else None,
            "battery_status": latest_log.battery_status if latest_log else "normal",
            "is_charging": latest_log.is_charging if latest_log else False,
            "last_synced": (latest_log.timestamp.isoformat() + "Z") if latest_log else None,
            "created_at": r.created_at.isoformat() + "Z"
        })
    return {"rings": result}


@app.post("/api/rings")
async def register_ring(payload: Dict[str, Any], db: Session = Depends(get_db)):
    """Register a new Oura Ring with custom label, emoji, color tag, and PAT."""
    label = str(payload.get("label", "")).strip() or "Oura Ring"
    emoji = str(payload.get("emoji", "💍")).strip() or "💍"
    color_tag = str(payload.get("color_tag", "teal")).strip() or "teal"
    token = str(payload.get("pat", "")).strip()

    if not token:
        raise HTTPException(status_code=400, detail="Personal Access Token cannot be empty.")

    validation = await oura_client.validate_token(token)
    if not validation.get("valid"):
        raise HTTPException(status_code=400, detail=validation.get("error", "Invalid Personal Access Token."))

    hw_type = "Oura Ring"
    fw_ver = "Latest"
    purchase_date = ""
    try:
        live = await oura_client.get_ring_battery(token_override=token)
        items = live.get("data", [])
        if items:
            latest = items[-1] if isinstance(items, list) else items
            hw_type = latest.get("hardware_type") or "Oura Ring"
            fw_ver = latest.get("firmware_version") or "Latest"
            set_up_at = latest.get("set_up_at", "")
            if set_up_at:
                try:
                    dt_setup = datetime.fromisoformat(set_up_at.replace("Z", "+00:00"))
                    purchase_date = dt_setup.strftime("%Y-%m-%d")
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Could not fetch initial ring battery metadata: {e}")

    ring = RingDevice(
        label=label,
        emoji=emoji,
        color_tag=color_tag,
        purchase_date=purchase_date,
        target_days=7.0,
        pat_token=token,
        hardware_type=hw_type,
        firmware_version=fw_ver,
        is_active=True
    )
    db.add(ring)
    db.commit()
    db.refresh(ring)

    inserted_count = await backfill_ring_history(ring, db, days_back=365)

    return {
        "message": f"Ring '{label}' registered successfully with {inserted_count} historical battery logs backfilled.",
        "ring": {
            "id": ring.id,
            "label": ring.label,
            "emoji": ring.emoji,
            "color_tag": ring.color_tag,
            "hardware_type": ring.hardware_type,
            "backfilled_records": inserted_count
        }
    }


@app.post("/api/rings/{ring_id}/backfill")
async def trigger_backfill(ring_id: int, db: Session = Depends(get_db)):
    """Trigger manual backfill of historical battery data for a ring."""
    ring = db.query(RingDevice).filter(RingDevice.id == ring_id, RingDevice.is_active == True).first()
    if not ring:
        raise HTTPException(status_code=404, detail="Ring profile not found.")

    count = await backfill_ring_history(ring, db, days_back=365)
    return {"message": f"Backfilled {count} historical battery logs for '{ring.label}'.", "count": count}


@app.put("/api/rings/{ring_id}")
async def update_ring(ring_id: int, payload: Dict[str, Any], db: Session = Depends(get_db)):
    """Update settings for an existing ring profile."""
    ring = db.query(RingDevice).filter(RingDevice.id == ring_id, RingDevice.is_active == True).first()
    if not ring:
        raise HTTPException(status_code=404, detail="Ring profile not found.")

    if "label" in payload:
        ring.label = str(payload["label"]).strip() or ring.label
    if "emoji" in payload:
        ring.emoji = str(payload["emoji"]).strip() or ring.emoji
    if "color_tag" in payload:
        ring.color_tag = str(payload["color_tag"]).strip() or ring.color_tag

    pat = str(payload.get("pat", "")).strip()
    if pat:
        validation = await oura_client.validate_token(pat)
        if not validation.get("valid"):
            raise HTTPException(status_code=400, detail=validation.get("error", "Invalid PAT token."))
        ring.pat_token = pat

    db.commit()
    db.refresh(ring)

    return {
        "message": f"Updated profile for '{ring.label}'.",
        "ring": {
            "id": ring.id,
            "label": ring.label,
            "emoji": ring.emoji,
            "color_tag": ring.color_tag,
            "purchase_date": ring.purchase_date,
            "target_days": ring.target_days
        }
    }


@app.delete("/api/rings/{ring_id}")
async def delete_ring(ring_id: int, db: Session = Depends(get_db)):
    """Deactivate/remove a registered ring profile."""
    ring = db.query(RingDevice).filter(RingDevice.id == ring_id).first()
    if not ring:
        raise HTTPException(status_code=404, detail="Ring device not found.")
    
    ring.is_active = False
    db.commit()
    return {"message": f"Ring '{ring.label}' removed."}


@app.get("/api/battery/metrics")
async def get_battery_metrics(ring_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    """Provides status, calculated discharge rate (%/day), remaining runtime, and health index for selected ring."""
    if ring_id:
        ring = db.query(RingDevice).filter(RingDevice.id == ring_id, RingDevice.is_active == True).first()
    else:
        ring = db.query(RingDevice).filter(RingDevice.is_active == True).first()

    target_baseline_rate = 14.3

    if not ring:
        return {
            "ring_id": None,
            "label": "Simulated Ring",
            "emoji": "💍",
            "color_tag": "teal",
            "battery_level": 78,
            "battery_status": "normal",
            "is_charging": False,
            "discharge_rate_per_day": 14.3,
            "is_calculated": False,
            "est_hours_remaining": 138,
            "est_days_remaining": 5.8,
            "battery_health_rating": "Excellent",
            "firmware_version": "2.1.3",
            "hardware_type": "Oura Ring 5 (Stealth Black, Size 10)",
            "last_synced": datetime.utcnow().isoformat() + "Z",
            "activation_date": "Jul 21, 2026",
            "is_mock": True
        }

    log_count = db.query(BatteryLog).filter(BatteryLog.ring_id == ring.id).count()
    if log_count < 5:
        await backfill_ring_history(ring, db, days_back=365)

    latest_db = db.query(BatteryLog).filter(BatteryLog.ring_id == ring.id, BatteryLog.battery_level > 0).order_by(BatteryLog.timestamp.desc()).first()

    if not latest_db:
        return {
            "ring_id": ring.id,
            "label": ring.label,
            "emoji": ring.emoji or "💍",
            "color_tag": ring.color_tag or "teal",
            "battery_level": 100,
            "battery_status": "normal",
            "is_charging": False,
            "discharge_rate_per_day": target_baseline_rate,
            "is_calculated": False,
            "est_hours_remaining": 168,
            "est_days_remaining": 7.0,
            "battery_health_rating": "Excellent",
            "firmware_version": ring.firmware_version or "Latest",
            "hardware_type": ring.hardware_type or "Oura Ring",
            "last_synced": datetime.utcnow().isoformat() + "Z",
            "activation_date": "",
            "is_mock": False
        }

    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    logs_24h = db.query(BatteryLog).filter(BatteryLog.ring_id == ring.id, BatteryLog.timestamp >= cutoff_24h, BatteryLog.battery_level > 0).order_by(BatteryLog.timestamp.asc()).all()
    
    discharge_rate = target_baseline_rate
    is_calculated = False
    if len(logs_24h) >= 2:
        first = logs_24h[0]
        last = logs_24h[-1]
        time_diff_hours = (last.timestamp - first.timestamp).total_seconds() / 3600.0
        level_diff = first.battery_level - last.battery_level
        if time_diff_hours > 1 and level_diff > 0:
            discharge_rate = round((level_diff / time_diff_hours) * 24.0, 1)
            is_calculated = True
    else:
        all_logs = db.query(BatteryLog).filter(BatteryLog.ring_id == ring.id, BatteryLog.battery_level > 0).order_by(BatteryLog.timestamp.asc()).all()
        if len(all_logs) >= 2:
            first = all_logs[0]
            last = all_logs[-1]
            time_diff_hours = (last.timestamp - first.timestamp).total_seconds() / 3600.0
            level_diff = first.battery_level - last.battery_level
            if time_diff_hours > 6 and level_diff > 0:
                discharge_rate = round((level_diff / time_diff_hours) * 24.0, 1)
                is_calculated = True

    est_days = round(latest_db.battery_level / (discharge_rate if discharge_rate > 0 else target_baseline_rate), 1)
    
    if discharge_rate <= 15.0:
        health_rating = "Excellent"
    elif discharge_rate <= 22.0:
        health_rating = "Good"
    elif discharge_rate <= 30.0:
        health_rating = "Fair"
    else:
        health_rating = "Degraded"

    activation_date = ""
    if ring.purchase_date:
        try:
            if "-" in ring.purchase_date and len(ring.purchase_date) == 10:
                p_date = datetime.strptime(ring.purchase_date, "%Y-%m-%d")
                activation_date = p_date.strftime("%b %d, %Y")
            elif "-" in ring.purchase_date:
                p_date = datetime.strptime(ring.purchase_date, "%Y-%m")
                activation_date = p_date.strftime("%b %Y")
            else:
                activation_date = ring.purchase_date
        except Exception:
            activation_date = ring.purchase_date

    return {
        "ring_id": ring.id,
        "label": ring.label,
        "emoji": ring.emoji or "💍",
        "color_tag": ring.color_tag or "teal",
        "purchase_date": ring.purchase_date or "",
        "target_days": 7.0,
        "battery_level": latest_db.battery_level,
        "battery_status": latest_db.battery_status,
        "is_charging": latest_db.is_charging,
        "discharge_rate_per_day": discharge_rate,
        "is_calculated": is_calculated,
        "est_hours_remaining": int(est_days * 24),
        "est_days_remaining": est_days,
        "battery_health_rating": health_rating,
        "firmware_version": latest_db.firmware_version or ring.firmware_version or "Latest",
        "hardware_type": latest_db.hardware_type or ring.hardware_type or "Oura Ring",
        "last_synced": latest_db.timestamp.isoformat() + "Z",
        "activation_date": activation_date,
        "is_mock": False
    }


@app.get("/api/battery/history")
async def get_battery_history(ring_id: Optional[int] = Query(None), days: int = Query(14, ge=1, le=365), db: Session = Depends(get_db)):
    if ring_id:
        ring = db.query(RingDevice).filter(RingDevice.id == ring_id, RingDevice.is_active == True).first()
    else:
        ring = db.query(RingDevice).filter(RingDevice.id == ring_id if ring_id else RingDevice.is_active == True).first()

    if not ring:
        now = datetime.utcnow()
        series = []
        steps = days * 2
        for i in range(steps, -1, -1):
            ts = now - timedelta(hours=i * 12)
            hours_offset = i * 12
            cycle_phase = (hours_offset * 0.6) % 85
            lvl = 88 + cycle_phase
            if lvl > 100:
                lvl = 15 + (lvl % 85)
            
            series.append({
                "timestamp": ts.isoformat() + "Z",
                "battery_level": min(100, max(12, int(lvl))),
                "battery_status": "charging" if lvl > 90 and i % 12 == 0 else "normal"
            })
        return {"history": series, "is_mock": True}

    cutoff = datetime.utcnow() - timedelta(days=days)
    logs = db.query(BatteryLog).filter(BatteryLog.ring_id == ring.id, BatteryLog.timestamp >= cutoff, BatteryLog.battery_level > 0).order_by(BatteryLog.timestamp.asc()).all()

    # Downsample logs to prevent Chart.js from freezing and to keep long-term charts clean
    if days > 7 and len(logs) > 100:
        # Min/Max Daily Downsampling for long time horizons
        from collections import defaultdict
        daily_logs = defaultdict(list)
        for log in logs:
            day_str = log.timestamp.strftime("%Y-%m-%d")
            daily_logs[day_str].append(log)
        
        downsampled = []
        for day_str, day_logs in daily_logs.items():
            if not day_logs:
                continue
            min_log = min(day_logs, key=lambda x: x.battery_level)
            max_log = max(day_logs, key=lambda x: x.battery_level)
            
            if min_log.timestamp < max_log.timestamp:
                downsampled.append(min_log)
                if min_log != max_log:
                    downsampled.append(max_log)
            else:
                downsampled.append(max_log)
                if min_log != max_log:
                    downsampled.append(min_log)
                    
        if downsampled and logs[-1] not in downsampled:
            downsampled.append(logs[-1])
        logs = downsampled
    else:
        # Standard decimation for <= 7 days
        max_points = 500
        if len(logs) > max_points:
            step = max(1, len(logs) // max_points)
            downsampled = []
            for i in range(0, len(logs), step):
                downsampled.append(logs[i])
            if logs[-1] not in downsampled:
                downsampled.append(logs[-1])
            logs = downsampled

    return {
        "history": [
            {
                "timestamp": log.timestamp.isoformat() + "Z",
                "battery_level": log.battery_level,
                "battery_status": log.battery_status
            }
            for log in logs
        ],
        "is_mock": False
    }


@app.get("/api/battery/drain_rate_history")
async def get_drain_rate_history(
    ring_id: Optional[int] = Query(None),
    days: int = Query(14, ge=1, le=365),
    db: Session = Depends(get_db)
):
    """Provides daily calculated battery drain rate (%/day) time series over N days."""
    if ring_id:
        ring = db.query(RingDevice).filter(RingDevice.id == ring_id, RingDevice.is_active == True).first()
    else:
        ring = db.query(RingDevice).filter(RingDevice.is_active == True).first()

    now = datetime.utcnow()

    if not ring:
        # Generate clean synthetic daily drain rate series for mock view
        daily_series = []
        base_rate = 14.0
        for i in range(days - 1, -1, -1):
            dt = now - timedelta(days=i)
            day_str = dt.strftime("%Y-%m-%d")
            noise = round(((days - i) * 0.35) % 3.2 - 1.2, 1)
            rate = round(max(9.0, min(32.0, base_rate + noise)), 1)
            
            if rate <= 16.0:
                status = "Healthy"
            elif rate <= 25.0:
                status = "Moderate Wear"
            else:
                status = "Degraded"

            daily_series.append({
                "date": day_str,
                "display_date": dt.strftime("%b %d"),
                "drain_rate_per_day": rate,
                "est_days_runtime": round(100.0 / rate, 1),
                "status": status
            })
        return {"daily_drain": daily_series, "is_mock": True}

    cutoff = now - timedelta(days=days)
    logs = db.query(BatteryLog).filter(
        BatteryLog.ring_id == ring.id,
        BatteryLog.timestamp >= cutoff,
        BatteryLog.battery_level > 0
    ).order_by(BatteryLog.timestamp.asc()).all()

    if not logs:
        daily_series = []
        for i in range(days - 1, -1, -1):
            dt = now - timedelta(days=i)
            daily_series.append({
                "date": dt.strftime("%Y-%m-%d"),
                "display_date": dt.strftime("%b %d"),
                "drain_rate_per_day": 14.3,
                "est_days_runtime": 7.0,
                "status": "Healthy"
            })
        return {"daily_drain": daily_series, "is_mock": True}

    from collections import defaultdict
    logs_by_date = defaultdict(list)
    for log in logs:
        logs_by_date[log.timestamp.strftime("%Y-%m-%d")].append(log)

    first_log = logs[0] if logs else None
    if first_log:
        first_date = first_log.timestamp.date()
        today_date = now.date()
        actual_days = (today_date - first_date).days + 1
        days_to_generate = min(days, max(1, actual_days))
    else:
        days_to_generate = days

    daily_series = []
    baseline_fallback = 14.3

    for i in range(days_to_generate - 1, -1, -1):
        target_dt = now - timedelta(days=i)
        target_date = target_dt.strftime("%Y-%m-%d")
        display_date = target_dt.strftime("%b %d")
        
        day_logs = logs_by_date.get(target_date, [])
        calculated_rate = None

        if len(day_logs) >= 2:
            non_charge_logs = [l for l in day_logs if not l.is_charging and l.battery_status != "charging"]
            if len(non_charge_logs) >= 2:
                first_log = non_charge_logs[0]
                last_log = non_charge_logs[-1]
                hours_diff = (last_log.timestamp - first_log.timestamp).total_seconds() / 3600.0
                level_diff = first_log.battery_level - last_log.battery_level
                
                if hours_diff >= 1.0 and level_diff > 0:
                    calculated_rate = round((level_diff / hours_diff) * 24.0, 1)

        if calculated_rate is None:
            calculated_rate = baseline_fallback

        baseline_fallback = calculated_rate

        if calculated_rate <= 16.0:
            status = "Healthy"
        elif calculated_rate <= 25.0:
            status = "Moderate Wear"
        else:
            status = "Degraded"

        daily_series.append({
            "date": target_date,
            "display_date": display_date,
            "drain_rate_per_day": calculated_rate,
            "est_days_runtime": round(100.0 / calculated_rate, 1) if calculated_rate > 0 else 7.0,
            "status": status
        })

    return {"daily_drain": daily_series, "is_mock": False}


@app.get("/api/battery/charges")
async def get_charge_events(ring_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    if ring_id:
        ring = db.query(RingDevice).filter(RingDevice.id == ring_id, RingDevice.is_active == True).first()
    else:
        ring = db.query(RingDevice).filter(RingDevice.id == ring_id if ring_id else RingDevice.is_active == True).first()

    if not ring:
        now = datetime.utcnow()
        return {
            "charges": [
                {
                    "id": 1,
                    "start_time": (now - timedelta(days=2)).isoformat() + "Z",
                    "end_time": (now - timedelta(days=2, hours=-1.2)).isoformat() + "Z",
                    "start_level": 15,
                    "end_level": 98,
                    "duration_minutes": 72,
                    "charge_speed_pct_per_hr": 69.2
                },
                {
                    "id": 2,
                    "start_time": (now - timedelta(days=9)).isoformat() + "Z",
                    "end_time": (now - timedelta(days=9, hours=-1.5)).isoformat() + "Z",
                    "start_level": 12,
                    "end_level": 100,
                    "duration_minutes": 90,
                    "charge_speed_pct_per_hr": 58.7
                }
            ],
            "is_mock": True
        }

    events = db.query(ChargeEvent).filter(ChargeEvent.ring_id == ring.id).order_by(ChargeEvent.start_time.desc()).all()
    formatted = []
    for ev in events:
        speed = 0.0
        if ev.duration_minutes and ev.duration_minutes > 0 and ev.end_level and ev.start_level:
            gained = ev.end_level - ev.start_level
            speed = round((gained / ev.duration_minutes) * 60.0, 1)
        formatted.append({
            "id": ev.id,
            "start_time": ev.start_time.isoformat() + "Z",
            "end_time": ev.end_time.isoformat() + "Z" if ev.end_time else None,
            "start_level": ev.start_level,
            "end_level": ev.end_level,
            "duration_minutes": ev.duration_minutes,
            "charge_speed_pct_per_hr": speed
        })

    return {"charges": formatted, "is_mock": False}


@app.get("/api/rings/comparison")
async def get_multi_ring_comparison(db: Session = Depends(get_db)):
    """Provides side-by-side battery health comparison for all registered active rings."""
    rings = db.query(RingDevice).filter(RingDevice.is_active == True).all()
    comparison = []
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)

    for r in rings:
        latest = db.query(BatteryLog).filter(BatteryLog.ring_id == r.id, BatteryLog.battery_level > 0).order_by(BatteryLog.timestamp.desc()).first()
        logs_24h = db.query(BatteryLog).filter(BatteryLog.ring_id == r.id, BatteryLog.timestamp >= cutoff_24h, BatteryLog.battery_level > 0).order_by(BatteryLog.timestamp.asc()).all()

        rate = 14.3
        if len(logs_24h) >= 2:
            f, l = logs_24h[0], logs_24h[-1]
            diff_h = (l.timestamp - f.timestamp).total_seconds() / 3600.0
            diff_lvl = f.battery_level - l.battery_level
            if diff_h > 1 and diff_lvl > 0:
                rate = round((diff_lvl / diff_h) * 24.0, 1)

        level = latest.battery_level if latest else 100
        est_days = round(level / rate, 1)

        comparison.append({
            "ring_id": r.id,
            "label": r.label,
            "emoji": r.emoji or "💍",
            "color_tag": r.color_tag or "teal",
            "hardware_type": r.hardware_type or "Oura Ring",
            "battery_level": level,
            "battery_status": latest.battery_status if latest else "normal",
            "is_charging": latest.is_charging if latest else False,
            "discharge_rate_per_day": rate,
            "est_days_remaining": est_days,
            "last_synced": (latest.timestamp.isoformat() + "Z") if latest else None
        })

    return {"comparison": comparison}


@app.get("/api/export/csv")
async def export_battery_logs_csv(ring_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    """Generates and downloads a CSV export of battery telemetry logs."""
    query = db.query(BatteryLog, RingDevice).join(RingDevice, BatteryLog.ring_id == RingDevice.id)
    if ring_id:
        query = query.filter(BatteryLog.ring_id == ring_id)
    
    logs = query.order_by(BatteryLog.timestamp.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Log ID", "Ring Label", "Timestamp (UTC)", "Battery Level (%)", "Battery Status", "Is Charging", "Hardware Type", "Firmware Version"])

    for log, ring in logs:
        writer.writerow([
            log.id,
            ring.label,
            log.timestamp.isoformat() + "Z",
            log.battery_level,
            log.battery_status,
            log.is_charging,
            log.hardware_type or ring.hardware_type,
            log.firmware_version or ring.firmware_version
        ])

    csv_data = output.getvalue()
    filename = f"oura_battery_logs_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
