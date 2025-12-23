import os
from dotenv import load_dotenv
from types import SimpleNamespace
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

# 1. Load your .env file
load_dotenv()
print(f"PK Loaded: {os.getenv('PRIVATE_KEY') is not None}")
print(f"Funder Loaded: {os.getenv('FUNDER_ADDRESS') is not None}")
print(f"API Key Loaded: {os.getenv('CLOB_API_KEY') is not None}")

def initialize_polymarket():
    # 1. Fetch values from .env
    # Ensure PRIVATE_KEY starts with '0x' in your .env file!
    pk = os.getenv("PRIVATE_KEY")
    funder = os.getenv("FUNDER_ADDRESS")
    
    # 2. Correct the parameter names here:
    # It must be 'api_key', 'api_secret', and 'api_passphrase'
    # creds = ApiCreds(
    #     api_key=os.getenv("CLOB_API_KEY"),
    #     api_secret=os.getenv("CLOB_SECRET"),
    #     api_passphrase=os.getenv("CLOB_PASSPHRASE")
    # )

    temp_client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, signature_type=2, funder=funder)
    creds = temp_client.create_or_derive_api_creds()

    # 3. Initialize the Client
    # host: Polymarket Order Book endpoint
    # chain_id: 137 (Polygon)
    # signature_type: 2 (Required for Phantom/MetaMask)
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=pk,
        chain_id=137,
        funder=funder,
        signature_type=2 
    )

    # 4. Apply credentials
    client.set_api_creds(creds)
    return client

if __name__ == "__main__":
    try:
        print("Checking connection to Polymarket...")
        client = initialize_polymarket()
        
        # Test the connection by fetching your account data
        # This will fail with 'Invalid Signature' if your keys are wrong
        status = client.get_ok()
        print(f"Server Response: {status}")
        print("✅ SUCCESS: Your keys and signature are valid!")

        # Verify the bot can see your balance
        # Note: This is an example, actual method name may vary by SDK version (e.g., get_balance)
        print("Initialization Successful! Bot is ready to sign trades.")
        
        try:
            # 1. Create the parameters as a SimpleNamespace instead of a dict
            # This allows the library to use .signature_type without crashing
            params = SimpleNamespace()
            params.asset_type = "COLLATERAL"
            params.signature_type = 2
            params.token_id = None
            # CORRECT METHOD: get_balance_allowance
            # This checks the USDC (Collateral) allowance for the funder address
            data = client.get_balance_allowance(params)
            
            # data usually looks like: {"balance": "100.0", "allowance": "1000000.0"}
            print(f"💰 USDC Balance:   ${data.get('balance')}")
        except Exception as e:
            print(f"Failed to update allowance: {e}")
    except Exception as e:
        print(f"Initialization Failed: {e}")
        print("\nTroubleshooting Checklist:")
        print("1. Does your Private Key start with '0x'?")
        print("2. Is signature_type set to 2?")
        print("3. Is the FUNDER_ADDRESS your Proxy Address (from Settings)?")