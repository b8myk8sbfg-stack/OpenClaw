import os
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()
tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

# We'll test the Siemens part number the AI just extracted
part_to_check = "Siemens 6ES7214-1AG40-0XB0"

print(f"Searching the web to verify: {part_to_check}...")

# This tells Tavily to find the official catalog or product page
search_result = tavily.search(query=part_to_check, search_depth="basic")

print("\n--- Search Results Found ---")
for result in search_result['results'][:3]: # Show top 3 results
    print(f"Title: {result['title']}")
    print(f"Link: {result['url']}\n")