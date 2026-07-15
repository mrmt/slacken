"""slacken.main は import 時に環境変数を読むためダミー値を先に設定"""
import os

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("NOTION_TOKEN", "secret_test")
os.environ.setdefault("GOOGLE_API_KEY", "gemini-test")
os.environ.setdefault("NOTION_DATABASE_ID", "db-test")
