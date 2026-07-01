import os
from dotenv import load_dotenv
from O365 import Account

load_dotenv()

credentials = (os.getenv('MICROSOFT_CLIENT_ID'), os.getenv('MICROSOFT_CLIENT_SECRET'))
tenant_id = os.getenv('MICROSOFT_TENANT_ID')

account = Account(credentials, auth_flow_type='credentials', tenant_id=tenant_id)

if account.authenticate():
    print("✅ Connection Verified!")
    
    # Try the .sg version first
    target_email = 'evon@robomatics.sg' 
    
    print(f"Attempting to read mailbox for: {target_email}...")
    try:
        mailbox = account.mailbox(resource=target_email)
        messages = mailbox.get_messages(limit=1)
        
        found = False
        for message in messages:
            found = True
            print(f"\n✅ SUCCESS! Connection is 100% Live.")
            print(f"Latest Email Subject: {message.subject}")
            print(f"From: {message.sender}")
        
        if not found:
            print("\nConnected, but the inbox appears to be empty.")
            
    except Exception as e:
        print(f"\n❌ Failed to find mailbox for {target_email}.")
        print(f"Error: {e}")
        print("\nACTION: Try changing the email to 'evon@robomatics.com.my' in the script and run again.")