"""
plc_to_postgres.py
------------------
อ่านค่า tag จาก PLC Wenger Extruder EX05 (Allen-Bradley ControlLogix, ผ่าน pycomm3 /
EtherNet-IP) แล้วเก็บลง PostgreSQL — รูปแบบเดียวกับ Coater_PLC_Interface/plc_to_postgres.py
เป๊ะ ๆ (thread เดียวพอเพราะมีแค่ line เดียว ไม่ต้อง multi-thread แบบ Coater ที่มี 5 line)

เขียนลง database เดียวกับ extrusion_plc_logger.js (Node, เก็บ EXT1-4 จาก Datablock) —
ตาราง plc_readings เดียวกัน ใช้ plc_name="Line5" ให้ตรงชุดกับ "Line1".."Line4" ที่มีอยู่แล้ว
เพื่อให้ pcg-server.js ฝั่ง /production-overview/live เปลี่ยนมาอ่าน EXT5 จากตารางนี้แทน
Historian ได้โดยไม่ต้องเพิ่ม connection ใหม่

ติดตั้ง dependency ก่อนใช้งาน:
    pip install pycomm3 psycopg2-binary

โครงสร้างตาราง: EAV (tag, value, timestamp) แบบเดียวกับทุกตัวเก็บ log อื่นในโปรเจกต์นี้ —
ยืดหยุ่นกว่าตารางแบบ column ตายตัว และเพิ่ม/ลบ tag ทีหลังได้โดยไม่ต้อง ALTER TABLE
tag ที่เป็น struct (Wen_ExtruderSpeed, ProductFormulaExtruder) จะถูกแตกเป็นหลาย row
(tag_name = "ProductFormulaExtruder.FormulaID" เป็นต้น) ไม่ใช่ dump ทั้งก้อนเป็น JSON —
ProductFormulaExtruder มีกว่า 150 field ส่วนใหญ่เป็น process setpoint ที่ไม่มีประโยชน์ตอนนี้
เก็บเฉพาะ field ที่ยืนยันแล้วว่ามีความหมายจริงกับ production overview พอ
"""

import argparse
import time
import logging
from datetime import datetime, timezone

from pycomm3 import LogixDriver
import psycopg2
from psycopg2.extras import execute_values

from db_config import PG_CONFIG, require_password

# ============================================================
# CONFIG
# ============================================================

PLC_IP = "10.232.145.90"
LINE_NAME = "Line5"  # ให้ตรงชุดกับ "Line1".."Line4" ที่ extrusion_plc_logger.js ใช้อยู่แล้ว

POLL_INTERVAL_SEC = 10.0  # เท่ากับ extrusion_plc_logger.js (Node) — ตั้งใจให้ freshness ใกล้เคียงกัน

TABLE_NAME = "plc_readings"

# tag ระดับบนที่ต้องอ่านจาก PLC (batch read) — ตัวที่เป็น struct จะถูกแตก field ใน to_rows()
# ชุด sensor (Wen_Extrudate*/Wen_*Press/Wen_LubeOilTemp/ExtruderLoad) verified เทียบกับหน้าจอ
# SCADA จริงแล้ว (2026-08-16) — ตรงเกือบเป๊ะทุกตัว ยกเว้น "Hygenic Hood Pressure" บนจอที่แท้จริง
# map ไปที่ Wen_FilterFanPressure ไม่ใช่ tag ชื่อ HygenicHood ตรง ๆ (ตัวนั้นอ่านได้ 0.0 ค้าง ไม่ใช้งานจริง)
TAGS = [
    "Wen_ProductionPhase",
    "Wen_ExtruderRate",
    "Wen_ExtruderSpeed",
    "ProductFormulaExtruder",
    "Extruder_Status",
    "RunAlarm",
    "Wen_ExtrudateTemp1",
    "Wen_ExtrudateTemp2",
    "Wen_KnifehoodTemp",
    "Wen_KnifehoodCoil",
    "Wen_KnifehoodCyclone",
    "Wen_ConeHead",
    "Wen_SteamPress",
    "Wen_CylSteamPress",
    "Wen_ExtSteamPress",
    "Wen_WaterPress",
    "Wen_AirPress",
    "Wen_LubeOilTemp",
    "Wen_FilterFanPressure",
    "ExtruderLoad",
    # "Product Output" cross-check — Totals.Dry_Feed accumulates dry-feed mass fed during the
    # CURRENT run (resets to 0 when a new run starts) and Totals.Dry_Feed_Prev holds the last
    # completed run's final total — the closest real accumulator Wenger exposes to BCTB's
    # ST_Stat/LT_Stat.Throughput_Extruder pair (verified live 2026-08-16: Dry_Feed=0.0 while
    # stopped, Dry_Feed_Prev=160175.6 from the last run). Not a literal "finished extrudate"
    # total (liquids/steam/water added on top aren't included), but the primary Product Output
    # figure on the report page comes from integrating Wen_ExtruderRate over time instead — this
    # is shown alongside it as a second, independently-sourced number to sanity-check against.
    # Also carries Liq_01-09/Ext_Steam/Ext_Water/Cyl_Steam/Cyl_Water — the same accumulated-total
    # shape for every steam/water/liquid addition channel, added below in STRUCT_FIELDS.
    "Totals",
    # Live dosing speed for every steam/water/liquid addition channel — verified live 2026-08-16
    # against the plant's real recipe (ProductFormulaExtruder.Liq4=5.0% was the only active
    # liquid channel; Wen_Liq4Speed read a small non-zero PV, the others read ~0), same CM_AI
    # struct shape as the sensor tiles above.
    "Wen_Liq1Speed", "Wen_Liq2Speed", "Wen_Liq3Speed", "Wen_Liq4Speed", "Wen_Liq5Speed",
    "Wen_Liq6Speed", "Wen_Liq7Speed", "Wen_Liq8Speed", "Wen_Liq9Speed",
    "Wen_ExtH2OSpeed", "Wen_CylH2OSpeed", "Wen_ExtSteamSpeed", "Wen_CylSteamSpeed",
]

# field ที่จะดึงออกมาจาก struct tag แต่ละตัว (key = ชื่อ field ใน pycomm3 dict, value = ชื่อ tag
# ที่จะเก็บลง DB เป็น "<TagName>.<field>") — ตัดเฉพาะที่ verified มีความหมายจริง ไม่ dump ทั้งก้อน
# CM_AI struct (Wen_* analog instruments) ใช้ field "PV" / InputDisp struct (ExtruderLoad) ใช้ "PVEngr"
STRUCT_FIELDS = {
    "Wen_ExtruderSpeed": ["PV"],
    "ProductFormulaExtruder": [
        "FormulaID", "Description", "Run_Number", "Revision", "Die_Num",
        # Recipe target % for every steam/water/liquid addition channel — verified live
        # 2026-08-16 (current recipe: Liq4=5.0%, CylH2O=12.8%, CylSteam=8.9%, ExtH2O=2.0%,
        # everything else 0 — inactive channels are filtered out on the report page, not shown
        # as a wall of zeroes).
        "Liq1", "Liq2", "Liq3", "Liq4", "Liq5", "Liq6", "Liq7", "Liq8", "Liq9",
        "CylSteam", "CylH2O", "ExtSteam", "ExtH2O",
    ],
    "Wen_ExtrudateTemp1": ["PV"],
    "Wen_ExtrudateTemp2": ["PV"],
    "Wen_KnifehoodTemp": ["PV"],
    "Wen_KnifehoodCoil": ["PV"],
    "Wen_KnifehoodCyclone": ["PV"],
    "Wen_ConeHead": ["PV"],
    "Wen_SteamPress": ["PV"],
    "Wen_CylSteamPress": ["PV"],
    "Wen_ExtSteamPress": ["PV"],
    "Wen_WaterPress": ["PV"],
    "Wen_AirPress": ["PV"],
    "Wen_LubeOilTemp": ["PV"],
    "Wen_FilterFanPressure": ["PV"],
    "ExtruderLoad": ["PVEngr"],
    "Totals": [
        "Dry_Feed", "Dry_Feed_Prev",
        "Liq_01", "Liq_02", "Liq_03", "Liq_04", "Liq_05", "Liq_06", "Liq_07", "Liq_08", "Liq_09",
        "Ext_Steam", "Ext_Water", "Cyl_Steam", "Cyl_Water",
    ],
    "Wen_Liq1Speed": ["PV"], "Wen_Liq2Speed": ["PV"], "Wen_Liq3Speed": ["PV"],
    "Wen_Liq4Speed": ["PV"], "Wen_Liq5Speed": ["PV"], "Wen_Liq6Speed": ["PV"],
    "Wen_Liq7Speed": ["PV"], "Wen_Liq8Speed": ["PV"], "Wen_Liq9Speed": ["PV"],
    "Wen_ExtH2OSpeed": ["PV"], "Wen_CylH2OSpeed": ["PV"],
    "Wen_ExtSteamSpeed": ["PV"], "Wen_CylSteamSpeed": ["PV"],
}

# Discrete/state tags — change-only ("deadband") logging, same technique extrusion_plc_logger.js
# (Node, EXT1-4) already uses for General.OperatingState/MainDrive.State/Cutter.StateMotor+Box:
# these sit unchanged for long stretches (verified live — Line1's MainDrive.State logged only 1
# row in 30 minutes), so writing an identical row every 10s is pure waste. The recipe's own
# fields barely ever change either, so they get the same treatment. Every analog measurement
# (rate/speed/motor load + all 13 sensor readouts) is deliberately NOT in this set — those keep
# logging every poll unconditionally, same reasoning as the Node logger's REAL-valued tags.
DISCRETE_TAGS = {
    "Wen_ProductionPhase", "RunAlarm", "Extruder_Status",
    "ProductFormulaExtruder.FormulaID", "ProductFormulaExtruder.Description",
    "ProductFormulaExtruder.Run_Number", "ProductFormulaExtruder.Revision",
    "ProductFormulaExtruder.Die_Num",
    "ProductFormulaExtruder.Liq1", "ProductFormulaExtruder.Liq2", "ProductFormulaExtruder.Liq3",
    "ProductFormulaExtruder.Liq4", "ProductFormulaExtruder.Liq5", "ProductFormulaExtruder.Liq6",
    "ProductFormulaExtruder.Liq7", "ProductFormulaExtruder.Liq8", "ProductFormulaExtruder.Liq9",
    "ProductFormulaExtruder.CylSteam", "ProductFormulaExtruder.CylH2O",
    "ProductFormulaExtruder.ExtSteam", "ProductFormulaExtruder.ExtH2O",
}


def filter_changed(rows, last_values):
    """Drops rows for DISCRETE_TAGS whose value_text is identical to the last row written for
    that tag — last_values is mutated in place so the caller just keeps passing the same dict
    back in every poll. Every tag outside DISCRETE_TAGS passes through unfiltered."""
    out = []
    for row in rows:
        tag_name, value_text = row[3], row[4]
        if tag_name in DISCRETE_TAGS:
            if last_values.get(tag_name) == value_text:
                continue
            last_values[tag_name] = value_text
        out.append(row)
    return out


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logging.getLogger("pycomm3").setLevel(logging.WARNING)
log = logging.getLogger("wenger_plc_to_postgres")


def parse_args():
    p = argparse.ArgumentParser(description="อ่าน tag จาก PLC Wenger Extruder EX05 แล้วเก็บลง PostgreSQL")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL_SEC,
                    help=f"วินาทีต่อรอบการอ่าน/บันทึก (default: {POLL_INTERVAL_SEC})")
    return p.parse_args()


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_pg_connection():
    return psycopg2.connect(**PG_CONFIG)


def ensure_table(conn):
    """สร้างตารางถ้ายังไม่มี — จริง ๆ แล้ว extrusion_plc_logger.js สร้างไว้แล้ว บล็อกนี้แค่กันเหนียว
    ถ้าเอาสคริปต์นี้ไปรันเทียบ database อื่นที่ยังไม่มีตาราง"""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL,
                plc_name    TEXT NOT NULL,
                plc_ip      TEXT NOT NULL,
                tag_name    TEXT NOT NULL,
                value_text  TEXT,
                value_num   DOUBLE PRECISION
            );
        """)
        # ใช้ชื่อเดียวกับ index ที่ extrusion_plc_logger.js / ตาราง plc_readings มีอยู่แล้ว
        # (ix_extrusion_readings_lookup) — เดิมชื่อ idx_plc_readings_plc_tag_ts ซึ่งซ้ำซ้อน 12 GB
        # และถูกลบไป 2026-10-06; ถ้าใช้ชื่อเดิม ทุกครั้งที่สคริปต์นี้ start จะสร้างใหม่แบบล็อกตาราง
        # ทำให้ logger ของ Extruder ทุก line เขียนข้อมูลไม่ได้จนกว่าจะสร้างเสร็จ
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS ix_extrusion_readings_lookup
            ON {TABLE_NAME} (plc_name, tag_name, ts DESC);
        """)
    conn.commit()
    log.info("ตรวจสอบ/สร้างตาราง %s เรียบร้อย", TABLE_NAME)


def insert_readings(conn, rows):
    """rows: list ของ tuple (timestamp, plc_name, plc_ip, tag_name, value_text, value_num)"""
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"INSERT INTO {TABLE_NAME} (ts, plc_name, plc_ip, tag_name, value_text, value_num) VALUES %s",
            rows,
        )
    conn.commit()


# ============================================================
# PLC READ HELPERS
# ============================================================

def to_rows(results, ts):
    """แปลงผลลัพธ์จาก pycomm3 เป็น rows พร้อม insert ลง postgres
    tag ที่เป็น struct (มี field ระบุใน STRUCT_FIELDS) จะถูกแตกเป็นหลาย row แยกตาม field
    ตัวที่เป็นค่าเดี่ยว (BOOL/REAL/DINT) เก็บเป็น 1 row ตามปกติ"""
    rows = []
    for r in results:
        if r.error:
            log.warning("อ่าน tag ไม่ได้: %s -> %s", r.tag, r.error)
            continue

        if r.tag in STRUCT_FIELDS:
            if not isinstance(r.value, dict):
                log.warning("คาดว่า %s จะเป็น struct แต่ได้ %s แทน — ข้าม", r.tag, type(r.value))
                continue
            for field in STRUCT_FIELDS[r.tag]:
                fval = r.value.get(field)
                value_num = float(fval) if isinstance(fval, (int, float)) and not isinstance(fval, bool) else None
                value_text = str(fval).replace("\x00", "") if fval is not None else None
                rows.append((ts, LINE_NAME, PLC_IP, f"{r.tag}.{field}", value_text, value_num))
            continue

        value = r.value
        value_num = None
        value_text = str(value).replace("\x00", "")
        if isinstance(value, bool):
            value_num = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            value_num = float(value)

        rows.append((ts, LINE_NAME, PLC_IP, r.tag, value_text, value_num))
    return rows


# ============================================================
# MAIN LOOP — line เดียว ไม่ต้อง multi-thread แบบ Coater
# ============================================================

def run(poll_interval):
    pg_conn = get_pg_connection()
    # Lives for the whole process, across PLC reconnects — resets only on process restart, same
    # as extrusion_plc_logger.js's in-memory last-value tracking.
    last_values = {}
    try:
        while True:
            try:
                with LogixDriver(PLC_IP) as plc:
                    if not plc.connected:
                        log.error("เชื่อมต่อ PLC ไม่สำเร็จ (%s) ตรวจสอบ IP/network — ลองใหม่ใน 5 วินาที", PLC_IP)
                        time.sleep(5)
                        continue

                    log.info("เชื่อมต่อ PLC สำเร็จ: %s (%s)", plc.info.get("product_name", "unknown"), PLC_IP)

                    # นับ read/insert ที่พังติดกัน — ถ้าเกิน CONSECUTIVE_FAILURE_LIMIT ให้ break
                    # ออกจาก loop ชั้นในเพื่อบังคับสร้าง LogixDriver connection ใหม่ (กลับไปเข้า
                    # `with LogixDriver(...)` ชั้นนอกอีกรอบ). ก่อนแก้ไข exception จาก plc.read()
                    # ถูก catch ไว้ที่ loop ชั้นในนี้เสมอ แล้ววนอ่านซ้ำบน connection เดิมที่เสียไป
                    # แล้วต่อไปเรื่อย ๆ ไม่เคยหลุดไปถึง except ชั้นนอก (บรรทัดล่าง ๆ) ที่ตั้งใจให้
                    # reconnect — ยืนยันจาก log จริง (wenger-plc-logger, 2026-09-14 09:29:54 เป็นต้น
                    # มา): "failed to send message" (แปลว่า TCP socket ไปยัง PLC ขาดไปแล้วจริง ๆ)
                    # ซ้ำทุก 10 วิ นานกว่า 23 ชม. โดยไม่มี "หลุดจาก PLC" ปรากฏใน log เลยสักครั้ง —
                    # ค้างอยู่แบบนี้ตลอดไปจนกว่าจะมีคน restart process เอง
                    consecutive_failures = 0
                    CONSECUTIVE_FAILURE_LIMIT = 3

                    while True:
                        loop_start = time.time()
                        ts = datetime.now(timezone.utc)
                        try:
                            results = plc.read(*TAGS)
                            rows = filter_changed(to_rows(results, ts), last_values)
                            insert_readings(pg_conn, rows)
                            log.info("บันทึกข้อมูลสำเร็จ %d row", len(rows))
                            consecutive_failures = 0
                        except Exception as e:
                            consecutive_failures += 1
                            log.error("เกิดข้อผิดพลาดระหว่าง read/insert (ติดกัน %d ครั้ง): %s", consecutive_failures, e)
                            try:
                                pg_conn.rollback()
                            except Exception:
                                # rollback() เองก็พัง แปลว่า pg_conn ตายจริง (เช่น Postgres ปิด
                                # connection ทิ้งตอนดิสก์เต็ม) — เดิม code จุดนี้ไม่เคยเปิด
                                # connection ใหม่เลย มีแต่ logic reconnect ฝั่ง PLC เท่านั้น ทำให้
                                # insert พังตลอดไปแม้ PLC จะเชื่อมต่อสำเร็จก็ตาม จนกว่าจะมีคน
                                # restart process เอง (เกิดขึ้นจริงกับ coater-plc-logger/
                                # wenger-plc-logger วันที่ 2026-08-28) — เปิด connection ใหม่แทน
                                log.error("PostgreSQL connection ใช้งานไม่ได้แล้ว — เปิด connection ใหม่")
                                try:
                                    pg_conn.close()
                                except Exception:
                                    pass
                                pg_conn = get_pg_connection()

                            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                                log.error(
                                    "read/insert พังติดกัน %d ครั้ง — ถือว่า PLC connection ตายแล้ว, จะปิดแล้วเชื่อมต่อใหม่",
                                    consecutive_failures,
                                )
                                break

                        elapsed = time.time() - loop_start
                        time.sleep(max(0, poll_interval - elapsed))
            except Exception as e:
                log.error("หลุดจาก PLC หรือเชื่อมต่อไม่ได้: %s — ลองใหม่ใน 5 วินาที", e)
                time.sleep(5)
    finally:
        pg_conn.close()
        log.info("ปิดการเชื่อมต่อ PostgreSQL แล้ว")


def main():
    require_password()
    args = parse_args()

    log.info("เชื่อมต่อ PostgreSQL เพื่อเตรียมตาราง ...")
    setup_conn = get_pg_connection()
    ensure_table(setup_conn)
    setup_conn.close()

    log.info("จะอ่าน %s (%s) ทุก %.0f วินาที — Ctrl+C เพื่อหยุด", LINE_NAME, PLC_IP, args.interval)
    try:
        run(args.interval)
    except KeyboardInterrupt:
        log.info("หยุดการทำงานตามคำสั่งผู้ใช้ (Ctrl+C)")


if __name__ == "__main__":
    main()
