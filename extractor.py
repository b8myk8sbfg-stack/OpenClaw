import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# Load your secret key
load_dotenv()

# Initialize the AI Brain
llm = ChatOpenAI(model="gpt-4o")

# A sample "dirty" email from a customer
sample_email = """
Dear Evon, I need a quote for 3 units of Siemens PLC 6ES7214-1AG40-0XB0 
and 1 unit of Omron power supply S8FS-G15024CD. 
Please check stock and price. Urgent!
"""

# The instruction for the AI
prompt = f"""
Extract the Brand, Part Number, and Quantity from this email. 
Output it as a clean list.

Email:
{sample_email}
"""

print("AI is thinking...")
response = llm.invoke(prompt)
print("\n--- Extracted Inquiry ---")
print(response.content)