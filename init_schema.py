from database import init_db
from flask import Flask

app = Flask(__name__)
with app.app_context():
    init_db(app)
print("Database initialized successfully.")
