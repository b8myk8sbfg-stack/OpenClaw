import pandas as pd
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()
llm = ChatOpenAI(model="gpt-4o")

def decide_strategy(requested_part):
    # 1. Check Warehouse
    inventory = pd.read_csv('warehouse_stock.csv')
    item = inventory[inventory['PartNumber'] == requested_part]
    
    if item.empty:
        return "Not in warehouse catalog. Action: Search for new suppliers."

    stock = item.iloc[0]['StockQuantity']
    equivalent = item.iloc[0]['EquivalentPart']

    # 2. Logic: If out of stock but has equivalent
    if stock == 0 and equivalent != "NONE":
        # Check if equivalent is in stock
        eq_item = inventory[inventory['PartNumber'] == equivalent]
        if not eq_item.empty and eq_item.iloc[0]['StockQuantity'] > 0:
            return f"Requested part {requested_part} is OUT OF STOCK. However, Equivalent {equivalent} is IN STOCK ({eq_item.iloc[0]['StockQuantity']} units). Action: Suggest equivalent to customer."
    
    elif stock > 0:
        return f"{requested_part} is IN STOCK ({stock} units). Action: Quote directly from warehouse."
    
    return "Out of stock, no equivalents. Action: Contact suppliers."

# Test the decision
requested = "6ES7214-1AG40-0XB0"
result = decide_strategy(requested)
print(f"Strategy for {requested}: \n{result}")

# Generate a professional pitch for the equivalent
if "Suggest equivalent" in result:
    print("\n--- AI Pitch for Equivalent ---")
    pitch = llm.invoke(f"The customer wants {requested} but we are out. We have the equivalent in stock. Write a professional 2-sentence offer to the customer suggesting the equivalent for a better price.").content
    print(pitch)