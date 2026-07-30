import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, Float, String, DateTime, Boolean, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./oura_tracker.db")

if DATABASE_URL.startswith("sqlite:///"):
    db_path = DATABASE_URL.replace("sqlite:///", "")
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class RingDevice(Base):
    __tablename__ = "ring_devices"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(100), nullable=False)
    emoji = Column(String(10), default="💍", nullable=False)
    color_tag = Column(String(20), default="teal", nullable=False)
    purchase_date = Column(String(20), nullable=True)  # e.g. "2024-10"
    target_days = Column(Float, default=7.0, nullable=False)
    pat_token = Column(String(255), nullable=False)
    hardware_type = Column(String(100), nullable=True)
    firmware_version = Column(String(50), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BatteryLog(Base):
    __tablename__ = "battery_logs"

    id = Column(Integer, primary_key=True, index=True)
    ring_id = Column(Integer, ForeignKey("ring_devices.id"), nullable=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    battery_level = Column(Integer, nullable=False)  # 0-100%
    battery_status = Column(String(50), nullable=True)  # normal, low, charging, full
    is_charging = Column(Boolean, default=False)
    firmware_version = Column(String(50), nullable=True)
    hardware_type = Column(String(50), nullable=True)


class ChargeEvent(Base):
    __tablename__ = "charge_events"

    id = Column(Integer, primary_key=True, index=True)
    ring_id = Column(Integer, ForeignKey("ring_devices.id"), nullable=True, index=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=True)
    start_level = Column(Integer, nullable=False)
    end_level = Column(Integer, nullable=True)
    duration_minutes = Column(Integer, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    
    # Auto-migration & seeder for multi-ring database
    db = SessionLocal()
    try:
        with engine.connect() as conn:
            # Migration check for battery_logs
            res_b = conn.execute(text("PRAGMA table_info(battery_logs);")).fetchall()
            cols_b = [row[1] for row in res_b]
            if "ring_id" not in cols_b:
                conn.execute(text("ALTER TABLE battery_logs ADD COLUMN ring_id INTEGER;"))
                conn.commit()

            # Migration check for charge_events
            res_c = conn.execute(text("PRAGMA table_info(charge_events);")).fetchall()
            cols_c = [row[1] for row in res_c]
            if "ring_id" not in cols_c:
                conn.execute(text("ALTER TABLE charge_events ADD COLUMN ring_id INTEGER;"))
                conn.commit()

            # Migration check for ring_devices new fields
            res_r = conn.execute(text("PRAGMA table_info(ring_devices);")).fetchall()
            cols_r = [row[1] for row in res_r]
            if "emoji" not in cols_r:
                conn.execute(text("ALTER TABLE ring_devices ADD COLUMN emoji VARCHAR(10) DEFAULT '💍';"))
                conn.commit()
            if "color_tag" not in cols_r:
                conn.execute(text("ALTER TABLE ring_devices ADD COLUMN color_tag VARCHAR(20) DEFAULT 'teal';"))
                conn.commit()
            if "purchase_date" not in cols_r:
                conn.execute(text("ALTER TABLE ring_devices ADD COLUMN purchase_date VARCHAR(20);"))
                conn.commit()
            if "target_days" not in cols_r:
                conn.execute(text("ALTER TABLE ring_devices ADD COLUMN target_days FLOAT DEFAULT 7.0;"))
                conn.commit()

        # Seed primary ring if PAT exists in environment and no rings registered
        pat = os.getenv("OURA_PAT")
        ring_count = db.query(RingDevice).count()
        if ring_count == 0 and pat:
            primary_ring = RingDevice(
                id=1,
                label="Primary Ring",
                emoji="💍",
                color_tag="teal",
                pat_token=pat,
                hardware_type="Oura Ring",
                firmware_version="Latest"
            )
            db.add(primary_ring)
            db.commit()
            
            # Associate pre-existing logs with primary_ring (ring_id = 1)
            db.query(BatteryLog).filter(BatteryLog.ring_id.is_(None)).update({"ring_id": 1})
            db.query(ChargeEvent).filter(ChargeEvent.ring_id.is_(None)).update({"ring_id": 1})
            db.commit()
    except Exception as e:
        print(f"Database migration notice: {e}")
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
