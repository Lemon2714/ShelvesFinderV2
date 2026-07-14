import csv
import os
import logging
from datetime import datetime
from typing import List
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()

load_dotenv()

logger = logging.getLogger(__name__)

# Use the root directory of the project for the `data` folder
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSV_FILE_PATH = os.path.join(ROOT_DIR, "data", "results.csv")

def save_result_to_csv(url: str, title: str, brand: str, product_id: str, keywords: List[str], branded_keywords: List[str], unbranded_keywords: List[str], browse_pages: List[str], openai_cost: float = 0.0) -> bool:
    """
    Appends the final analysis result and API cost to a local CSV file.
    Creates the file and headers if it doesn't already exist.
    """
    try:
        os.makedirs(os.path.dirname(CSV_FILE_PATH), exist_ok=True)
        file_exists = os.path.isfile(CSV_FILE_PATH)
        
        with open(CSV_FILE_PATH, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Date", "Input URL", "Title", "Brand", "Product ID", "All Keywords", "Branded Keywords", "Unbranded Keywords", "Category Pages", "OpenAI Cost"])
            
            # Format lists into clean strings
            kw_str = ", ".join(keywords)
            b_kw_str = ", ".join(branded_keywords)
            u_kw_str = ", ".join(unbranded_keywords)
            bp_str = "\n".join(browse_pages)
            
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([current_date, url, title, brand, product_id, kw_str, b_kw_str, u_kw_str, bp_str, f"{openai_cost:.6f}"])
            
        logger.info(f"Successfully appended result to {CSV_FILE_PATH}")
        return True
    except Exception as e:
        logger.error(f"Failed to append to CSV: {e}", exc_info=True)
        return False

def upload_csv_to_drive(file_path: str = CSV_FILE_PATH) -> bool:
    """Uploads the specified CSV file to Google Drive using a Service Account."""
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        logger.warning("GOOGLE_DRIVE_FOLDER_ID is missing. Skipping Drive upload.")
        return False
        
    try:
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=credentials)
        
        # Check if results.csv already exists in the folder
        query = f"name='results.csv' and '{folder_id}' in parents and trashed=false"
        results = service.files().list(
            q=query, 
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        items = results.get('files', [])
        
        media = MediaFileUpload(file_path, mimetype='text/csv')
        
        if items:
            # File exists, update it
            file_id = items[0]['id']
            service.files().update(
                fileId=file_id, 
                media_body=media,
                supportsAllDrives=True
            ).execute()
            logger.info(f"Successfully updated file in Google Drive (ID: {file_id})")
        else:
            # File doesn't exist, create it
            file_metadata = {
                'name': 'results.csv',
                'parents': [folder_id]
            }
            file = service.files().create(
                body=file_metadata, 
                media_body=media, 
                fields='id',
                supportsAllDrives=True
            ).execute()
            logger.info(f"Successfully uploaded new file to Google Drive (ID: {file.get('id')})")
        return True
    except Exception as e:
        logger.error(f"Failed to upload to Google Drive: {e}", exc_info=True)
        return False

def append_result_to_sheet(url: str, title: str, brand: str, product_id: str, keywords: List[str], branded_keywords: List[str], unbranded_keywords: List[str], browse_pages: List[str], openai_cost: float = 0.0) -> bool:
    """Natively appends a row to a Google Sheet without downloading anything."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        logger.warning("GOOGLE_SHEET_ID is missing. Skipping Google Sheets append.")
        return False
        
    try:
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        service = build('sheets', 'v4', credentials=credentials)
        
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        kw_str = ", ".join(keywords)
        b_kw_str = ", ".join(branded_keywords)
        u_kw_str = ", ".join(unbranded_keywords)
        bp_str = "\n".join(browse_pages)
        
        values = [
            [current_date, url, title, brand, product_id, kw_str, b_kw_str, u_kw_str, bp_str, f"{openai_cost:.6f}"]
        ]
        body = {'values': values}
        
        result = service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="Sheet1!A:J",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        
        logger.info(f"Successfully appended row to Google Sheet (Updates: {result.get('updates', {})})")
        return True
    except Exception as e:
        logger.error(f"Failed to append to Google Sheet: {e}", exc_info=True)
        return False
