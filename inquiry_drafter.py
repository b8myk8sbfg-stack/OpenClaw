from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
import os

load_dotenv()
llm = ChatOpenAI(model="gpt-4o")

def draft_supplier_email(brand, part_number, quantity):
    prompt = f"""
    Write a professional formal inquiry email to a supplier.
    We are looking for: {brand} {part_number}
    Quantity: {quantity}
    Ask for: Best price, Lead time, and Shipping cost to our warehouse.
    Sign off as: Sales Team - Open Claw Industrial
    """
    response = llm.invoke(prompt)
    return response.content

# Test the draft
email_content = draft_supplier_email("Siemens", "6ES7214-1AG40-0XB0", "3 units")
print("\n--- Drafted Supplier Email ---")
print(email_content)