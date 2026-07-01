import pandas as pd

# This function mimics searching your accounting records
def check_past_sales(brand, part_number):
    # Load your "Accounting Database"
    try:
        df = pd.read_csv('sales_history.csv')
        
        # Look for a match
        match = df[df['PartNumber'] == part_number]
        
        if not match.empty:
            supplier = match.iloc[0]['PreviousSupplier']
            price = match.iloc[0]['PurchasePrice']
            return f"✅ Match Found! Previously bought from {supplier} at ${price}."
        else:
            return "❌ No record of this part in our history."
    except Exception as e:
        return f"Error reading history: {e}"

# Let's test it with the part numbers we found earlier
print("Checking history for Siemens 6ES7214-1AG40-0XB0...")
print(check_past_sales("Siemens", "6ES7214-1AG40-0XB0"))

print("\nChecking history for a new part (ABC-123)...")
print(check_past_sales("Unknown", "ABC-123"))