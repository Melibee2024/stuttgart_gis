import os
from dotenv import load_dotenv

# Load the .env file from the root 'planung' folder
load_dotenv()

# Securely grab credentials from your .env
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST", "localhost")  # Defaults to localhost if not found

# Your specific project database configuration
DB_HFT = {
    'HOST': DB_HOST,
    'PORT': 5432,
    'NAME': "hft_db",
    'USER': DB_USER,
    'PASS': DB_PASS,
}