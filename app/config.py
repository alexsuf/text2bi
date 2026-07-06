import os

class Config:
    REDASH_URL = os.environ.get('REDASH_URL', 'http://server:5000/api/queries')
    REDASH_API_KEY = os.environ.get('REDASH_API_KEY', 'dcmjDqSLz1QW1hrCfW72FJOYM6Li1KCp45sB6ZmP')
    REDASH_DATA_SOURCE_ID = int(os.environ.get('REDASH_DATA_SOURCE_ID', '2'))
    
    LLM_MODEL = ""
    LLM_BASE_URL = ""
    LLM_API_KEY = ""
    
    SECRET_KEY = "your-secret-key-change-in-production"
    DOWNLOADS_DIR = "./downloads"
