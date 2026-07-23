import psycopg2
import os
import time

class Database:
    def __init__(self):
        # تلاش برای خواندن DATABASE_URL و اگر نبود DATABASE_PUBLIC_URL
        self.db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")
        
        if not self.db_url:
            print("❌ CRITICAL ERROR: No Database URL found in environment variables!")
            return

        # اصلاح فرمت آدرس برای پایتون
        if self.db_url.startswith("postgres://"):
            self.db_url = self.db_url.replace("postgres://", "postgresql://", 1)

        # تلاش برای اتصال
        for i in range(3):
            try:
                self.conn = psycopg2.connect(self.db_url)
                self.conn.autocommit = True
                print("✅ Database connected successfully!")
                break
            except Exception as e:
                print(f"Database connection attempt {i+1} failed: {e}")
                time.sleep(2)
        else:
            print("❌ Could not connect to PostgreSQL after 3 attempts.")
            return

        self.create_tables()

    def create_tables(self):
        with self.conn.cursor() as cursor:
            cursor.execute('''CREATE TABLE IF NOT EXISTS tracked_users 
                              (username TEXT PRIMARY KEY, last_id TEXT)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS subscriptions 
                              (chat_id TEXT, username TEXT, PRIMARY KEY(chat_id, username))''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS sent_ids 
                              (chat_id TEXT, tweet_id TEXT, PRIMARY KEY(chat_id, tweet_id))''')

    def get_all_tracked(self):
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT username, last_id FROM tracked_users")
            return cursor.fetchall()

    def update_last_id(self, username, last_id):
        with self.conn.cursor() as cursor:
            cursor.execute("UPDATE tracked_users SET last_id = %s WHERE username = %s", (last_id, username))

    def get_subs_for_user(self, username):
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT chat_id FROM subscriptions WHERE username = %s", (username,))
            return [row[0] for row in cursor.fetchall()]

    def is_subscribed(self, chat_id, username):
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM subscriptions WHERE chat_id = %s AND username = %s", (str(chat_id), username))
            return cursor.fetchone() is not None

    def add_subscription(self, chat_id, username, last_id=""):
        with self.conn.cursor() as cursor:
            cursor.execute("INSERT INTO tracked_users (username, last_id) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING", (username, last_id))
            cursor.execute("INSERT INTO subscriptions (chat_id, username) VALUES (%s, %s) ON CONFLICT (chat_id, username) DO NOTHING", (str(chat_id), username))

    def remove_subscription(self, chat_id, username):
        with self.conn.cursor() as cursor:
            cursor.execute("DELETE FROM subscriptions WHERE chat_id = %s AND username = %s", (str(chat_id), username))
            cursor.execute("SELECT 1 FROM subscriptions WHERE username = %s", (username,))
            if not cursor.fetchone():
                cursor.execute("DELETE FROM tracked_users WHERE username = %s", (username,))

    def is_duplicate(self, chat_id, tweet_id):
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM sent_ids WHERE chat_id = %s AND tweet_id = %s", (str(chat_id), str(tweet_id)))
            return cursor.fetchone() is not None

    def mark_sent(self, chat_id, tweet_id):
        with self.conn.cursor() as cursor:
            cursor.execute("INSERT INTO sent_ids (chat_id, tweet_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (str(chat_id), str(tweet_id)))
