import urequests as requests
import gc

class TelegramBot:
    def __init__(self, token, auth_chat_id=None):
        self.token = token
        # If auth_chat_id is provided, convert to int, else stay None
        self.auth_id = int(auth_chat_id) if auth_chat_id else None
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.offset = 0

    def send_message(self, message, chat_id=None):
        """Sends a message. Prioritizes provided chat_id, then auth_id."""
        target_id = chat_id if chat_id else self.auth_id
        if not target_id: return

        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": target_id, "text": message}
        
        response = None
        try:
            response = requests.post(url, json=payload)
        except Exception as e:
            print(f"Send failed: {e}")
        finally:
            if response:
                response.close() # Clean up manually
            gc.collect()
    def request_data(self):
        """
        Polls for new commands. 
        Returns (text, sender_id) for the latest message, or (None, None).
        """
        url = f"{self.base_url}/getUpdates?offset={self.offset}&timeout=5"
        response = None
        data = None 
        try:
            response = requests.get(url)
            data = response.json()
        except Exception as e:
            print(f"Polling error: {e}")
            return None, None
        finally:
            if response:
                response.close() 


        if not data.get('ok') or not data.get('result'):
            return None, None
        
        # We process the latest message
        update = data['result'][-1]
        self.offset = update['update_id'] + 1
        
        message = update.get('message', {})
        sender_id = message.get('from', {}).get('id')
        text = message.get('text', "")

        return text, sender_id