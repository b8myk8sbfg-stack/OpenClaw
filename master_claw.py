import os
import pandas as pd
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from tavily import TavilyClient

# 1. Setup
load_dotenv()
llm = ChatOpenAI(model="gpt-4o")
tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

def run_open_claw(email_body):
    print("--- 🚀 OPEN CLAW SYSTEM STARTING ---")
    
    # PHASE 1: EXTRACTION
    print("\n[Phase 1] Analyzing Email...")
    extract_prompt = f"Extract Brand and Part Number from: {email_body}. Return as 'Brand: PartNumber'"
    extracted = llm.invoke(extract_prompt).content
    print(f"Result: {extracted}")

    # For this demo, let's assume we split the result into brand and part
    # In a real app, we'd use more complex parsing, but let's keep it simple for now:
    brand = "Siemens"
    part = "6ES7214-1AG40-0XB0"

    # PHASE 2: HISTORY CHECK
    print("\n[Phase 2] Checking Accounting History...")
    df = pd.read_csv('sales_history.csv')
    match = df[df['PartNumber'] == part]
    
    if not match.empty:
        print(f"Found Record! Previous Supplier: {match.iloc[0]['PreviousSupplier']}")
    else:
        print("No previous record found.")

    # PHASE 3: SUPPLIER SEARCH
    print(f"\n[Phase 3] Finding new suppliers for {brand}...")
    search = tavily.search(query=f"distributor for {brand} {part}", max_results=3)
    for s in search['results']:
        print(f"Found: {s['title']}")

    # PHASE 4: DRAFTING
    print("\n[Phase 4] Drafting Outreach...")
    draft = llm.invoke(f"Write a short inquiry for 3 units of {brand} {part} to these suppliers.").content
    print("\nFINAL DRAFT READY:")
    print(draft)
    print("\n--- ✅ PROCESS COMPLETE ---")

# Let's test the whole loop!
customer_email = "Hi, I need 3 of the Siemens PLC 6ES7214-1AG40-0XB0. Urgent quote please."
run_open_claw(customer_email)