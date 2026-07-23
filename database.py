import sqlite3
import os

class Database:
    def __init__(self, db_path="data/bot_data.db"):
        # ابتدا مطمئن می‌شویم پوشه وجود دارد
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS tracked_users 
                          (username TEXT PRIMARY KEY, last_id TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS subscriptions 
                          (chat_id TEXT, username TEXT, PRIMARY KEY(chat_id, username))''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS sent_ids 
                          (chat_id TEXT, tweet_id TEXT, PRIMARY KEY(chat_id, tweet_id))''')
        self.conn.commit()

    def get_all_tracked(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT username, last_id FROM tracked_users")
        return cursor.fetchall()

    def update_last_id(self, username, last_id):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE tracked_users SET last_id = ? WHERE username = ?", (last_id, username))
        self.conn.commit()

    def get_subs_for_user(self, username):
        cursor = self.conn.cursor()
        cursor.execute("SELECT chat_id FROM subscriptions WHERE username = ?", (username,))
        return [row[0] for row in cursor.fetchall()]

    def is_duplicate(self, chat_id, tweet_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM sent_ids WHERE chat_id = ? AND tweet_id = ?", (str(chat_id), str(tweet_id)))
        return cursor.fetchone() is not None

    def mark_sent(self, chat_id, tweet_id):
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO sent_ids (chat_id, tweet_id) VALUES (?, ?)", (str(chat_id), str(tweet_id)))
        self.conn.commit()
        
    def add_subscription(self, chat_id, username):
        cursor = self.conn.cursor()
        # افزودن یوزر به لیست مانیتورینگ عمومی اگر وجود ندارد
        cursor.execute("INSERT OR IGNORE INTO tracked_users (username, last_id) VALUES (?, ?)", (username, ""))
        # افزودن اشتراک چت خاص
        cursor.execute("INSERT OR IGNORE INTO subscriptions (chat_id, username) VALUES (?, ?)", (str(chat_id), username))
        self.conn.commit()
