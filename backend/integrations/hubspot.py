import json
import secrets
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import base64
import requests

from integrations.integration_item import IntegrationItem

from redis_client import add_key_value_redis, get_value_redis, delete_key_redis
from dotenv import load_dotenv
import os

load_dotenv()  # loads .env file

CLIENT_ID = os.getenv('HUBSPOT_CLIENT_ID')
CLIENT_SECRET = os.getenv('HUBSPOT_CLIENT_SECRET')
# HubSpot OAuth Configuration

REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'

# HubSpot OAuth scopes - adjust based on what data you need
SCOPES = 'oauth crm.objects.contacts.read crm.objects.companies.read crm.objects.deals.read tickets'

# URL encode the redirect URI and scopes properly
import urllib.parse
encoded_redirect_uri = urllib.parse.quote(REDIRECT_URI, safe='')
encoded_scopes = urllib.parse.quote(SCOPES, safe='')

authorization_url = f'https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={encoded_redirect_uri}&scope={encoded_scopes}'

async def authorize_hubspot(user_id, org_id):
    """Generate HubSpot OAuth authorization URL"""
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = base64.urlsafe_b64encode(json.dumps(state_data).encode('utf-8')).decode('utf-8')
    
    # Store state in Redis for verification
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', json.dumps(state_data), expire=600)
    
    # URL encode the state parameter
    import urllib.parse
    encoded_state_param = urllib.parse.quote(encoded_state, safe='')
    auth_url = f'{authorization_url}&state={encoded_state_param}'
    return auth_url

async def oauth2callback_hubspot(request: Request):
    """Handle HubSpot OAuth callback"""
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error_description'))
    
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    
    if not code or not encoded_state:
        raise HTTPException(status_code=400, detail='Missing code or state parameter')
    
    try:
        state_data = json.loads(base64.urlsafe_b64decode(encoded_state).decode('utf-8'))
    except:
        raise HTTPException(status_code=400, detail='Invalid state parameter')

    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')

    # Verify state
    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')
    if not saved_state or original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')

    # Exchange code for access token
    async with httpx.AsyncClient() as client:
        token_response, _ = await asyncio.gather(
            client.post(
                'https://api.hubapi.com/oauth/v1/token',
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': REDIRECT_URI,
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                },
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                }
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}'),
        )

    if token_response.status_code != 200:
        raise HTTPException(status_code=400, detail='Failed to exchange code for token')

    # Store credentials temporarily
    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(token_response.json()), expire=600)
    
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    """Retrieve and return HubSpot credentials"""
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    
    credentials = json.loads(credentials)
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')
    
    return credentials

def create_integration_item_metadata_object(
    response_json: dict, item_type: str, parent_id=None, parent_name=None
) -> IntegrationItem:
    """Create IntegrationItem from HubSpot API response"""
    
    # Extract name based on item type
    name = None
    if item_type == 'Contact':
        firstname = response_json.get('properties', {}).get('firstname', '')
        lastname = response_json.get('properties', {}).get('lastname', '')
        email = response_json.get('properties', {}).get('email', '')
        name = f"{firstname} {lastname}".strip() or email or f"Contact {response_json.get('id', 'Unknown')}"
    elif item_type == 'Company':
        name = response_json.get('properties', {}).get('name', f"Company {response_json.get('id', 'Unknown')}")
    elif item_type == 'Deal':
        name = response_json.get('properties', {}).get('dealname', f"Deal {response_json.get('id', 'Unknown')}")
    elif item_type == 'Ticket':
        name = response_json.get('properties', {}).get('subject', f"Ticket {response_json.get('id', 'Unknown')}")
    else:
        name = f"{item_type} {response_json.get('id', 'Unknown')}"

    integration_item_metadata = IntegrationItem(
        id=f"{response_json.get('id')}_{item_type}",
        name=name,
        type=item_type,
        parent_id=parent_id,
        parent_path_or_name=parent_name,
        creation_time=response_json.get('createdAt'),
        last_modified_time=response_json.get('updatedAt'),
        url=f"https://app.hubspot.com/contacts/{response_json.get('id')}"
    )

    return integration_item_metadata

async def get_items_hubspot(credentials) -> list[IntegrationItem]:
    """Fetch items from HubSpot and return as IntegrationItem objects"""
    if isinstance(credentials, str):
        credentials = json.loads(credentials)
    access_token = credentials.get('access_token')
    
    if not access_token:
        raise HTTPException(status_code=400, detail='No access token found in credentials')

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    list_of_integration_items = []

    # Define the endpoints and their corresponding types
    endpoints = [
        ('https://api.hubapi.com/crm/v3/objects/contacts', 'Contact'),
        ('https://api.hubapi.com/crm/v3/objects/companies', 'Company'),
        ('https://api.hubapi.com/crm/v3/objects/deals', 'Deal'),
        ('https://api.hubapi.com/crm/v3/objects/tickets', 'Ticket'),
    ]

    try:
        for endpoint_url, item_type in endpoints:
            # Fetch data from each endpoint
            print(f"Fetching {item_type}s from {endpoint_url}")
            response = requests.get(
                endpoint_url,
                headers=headers,
                params={'limit': 100}  # Limit to 100 items per type for demo
            )
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                print(f"Found {len(results)} {item_type}s")
                
                for item in results:
                    integration_item = create_integration_item_metadata_object(
                        item, item_type
                    )
                    list_of_integration_items.append(integration_item)
            else:
                print(f"Failed to fetch {item_type}s: {response.status_code}")
                if response.status_code == 403:
                    print(f"Access denied for {item_type}s - check your HubSpot app permissions")

    except Exception as e:
        print(f"Error fetching HubSpot items: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching HubSpot items: {str(e)}")

    print(f'HubSpot integration items count: {len(list_of_integration_items)}')
    return list_of_integration_items