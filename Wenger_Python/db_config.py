"""
db_config.py
------------
ตั้งค่าการเชื่อมต่อ PostgreSQL ให้ plc_to_postgres.py และ test_read_plc.py ใช้ร่วมกัน
อ่านค่าจาก environment variable แทนการฝัง password ตรงในโค้ด — ไฟล์นี้จะถูก push ขึ้น git
ด้วย ห้ามใส่ password จริงลงในไฟล์นี้เด็ดขาด

ตั้งใจชี้ไปที่ database `extrusion_history_data` เดียวกับที่ extrusion_plc_logger.js (Node,
เก็บข้อมูล EXT1-4 จาก Datablock) ใช้อยู่แล้ว — EX05/Wenger เป็น "Line5" ในตาราง plc_readings
เดียวกัน ไม่ใช่ database แยก เพื่อให้ pcg-server.js ฝั่ง /production-overview/live query
ทั้ง 5 line จากที่เดียวกันได้ในอนาคต แทนที่จะต้องรวมสองแหล่งข้อมูลคนละ database

วิธีตั้งค่าก่อนรันจริงบนเครื่อง (Windows PowerShell):
    $env:WENGER_PG_PASSWORD = "รหัสผ่านจริงของ extrusion_user"
    python plc_to_postgres.py

ตัวแปรอื่นที่ปรับได้:
    WENGER_PG_HOST, WENGER_PG_PORT, WENGER_PG_DBNAME, WENGER_PG_USER
"""

import os

PG_CONFIG = {
    "host": os.environ.get("WENGER_PG_HOST", "localhost"),
    "port": int(os.environ.get("WENGER_PG_PORT", "5432")),
    "dbname": os.environ.get("WENGER_PG_DBNAME", "extrusion_history_data"),
    "user": os.environ.get("WENGER_PG_USER", "extrusion_user"),
    "password": os.environ.get("WENGER_PG_PASSWORD", ""),
}


def require_password():
    """เรียกก่อนเชื่อมต่อจริง — เตือนชัดเจนถ้าลืมตั้ง WENGER_PG_PASSWORD แทนที่จะปล่อยให้ psycopg2 error มั่ว ๆ"""
    if not PG_CONFIG["password"]:
        raise RuntimeError(
            "ยังไม่ได้ตั้งค่า WENGER_PG_PASSWORD — ตั้งก่อนรัน เช่น:\n"
            '  $env:WENGER_PG_PASSWORD = "รหัสผ่านจริงของ extrusion_user"\n'
            "ดูรายละเอียดใน db_config.py / README.md"
        )
