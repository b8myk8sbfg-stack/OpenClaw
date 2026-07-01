import os
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()
tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

def find_suppliers(brand, part_number):
    query = f"Where to buy {brand} {part_number} official distributor wholesaler"
    print(f"Searching for 3+ suppliers for: {brand}...")
    
    search_result = tavily.search(query=query, search_depth="advanced", max_results=5)
    
    suppliers = []
    for result in search_result['results']:
        suppliers.append({
            "title": result['title'],
            "url": result['url']
        })
    return suppliers

# Test the logic
brand_test = "Siemens"
part_test = "6ES7214-1AG40-0XB0"
found_list = find_suppliers(brand_test, part_test)

print("\n--- Potential Suppliers Found ---")
for i, s in enumerate(found_list, 1):
    print(f"{i}. {s['title']}")
    print(f"   Link: {s['url']}")