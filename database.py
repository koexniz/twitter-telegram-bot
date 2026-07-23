import psycopg2
import os
import time

class Database:
    def __init__(self):
        # دیباگ: چاپ تمام متغیرهای موجود (فقط نام‌ها)
        print("Available environment variables:", list(os.environ.keys()))
        
        self.db_url = os.getenv("DATABASE_URL")
        
        if not self.db_url:
            # اگر متغیر نبود، به جای کرش، فعلاً یه هشدار بده تا لاگ رو ببینیم
            print("❌ ERROR: DATABASE_URL NOT FOUND!")
            # اگر لوکال هستید، می‌توانید دستی اینجا آدرس بدهید (برای تست)
            # self.db_url = "آدرس دیتابیس خود را اینجا بگذارید"
            return 

        if self.db_url.startswith("postgres://"):
            self.db_url = self.db_url.replace("postgres://", "postgresql://", 1)

        try:
            self.conn = psycopg2.connect(self.db_url)
            self.conn.autocommit = True
            self.create_tables()
            print("✅ Database connected successfully!")
        except Exception as e:
            print(f"❌ Database connection failed: {e}")
