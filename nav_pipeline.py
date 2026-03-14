# Imports

import pandas as pd
import psycopg2
import os
from dotenv import load_dotenv


# Load env vars
load_dotenv()

HOST = os.environ.get("host")
USER = os.environ.get("user")
PASSWORD = os.environ.get("password")
PORT = os.environ.get("port")
NAME = os.environ.get("dbname")
