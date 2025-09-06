# Airtable Integration - Complete Technical Guide

## 🎯 Overview
This guide explains how the Airtable integration works in our application. Airtable is a cloud-based database platform that combines the simplicity of a spreadsheet with the power of a database. Our integration allows users to securely connect their Airtable account and fetch their bases and tables.

## 🏗️ Architecture Overview

```
Frontend (React) ↔ Backend (FastAPI) ↔ Redis (Storage) ↔ Airtable API
```

**Data Flow**: 
- **Frontend**: React components handle user interactions
- **Backend**: FastAPI server manages OAuth flow and API calls
- **Redis**: Temporary storage for OAuth tokens and state
- **Airtable API**: External database service providing structured data

## 🔄 Complete Flow Explanation

### The Big Picture:
1. User clicks "Connect to Airtable" → OAuth authorization starts with PKCE security
2. User authorizes in Airtable → We receive authorization code
3. Backend exchanges code for access token → Credentials stored temporarily
4. User clicks "Load Data" → We fetch bases and their tables
5. Data is displayed → User sees hierarchical structure of bases and tables

---

## 📋 The 4 Airtable Endpoints Explained

### 1. `/integrations/airtable/authorize` - START THE CONNECTION

**What it does**: Creates a secure authorization URL with PKCE (Proof Key for Code Exchange)

**Step-by-Step Process**:

```python
async def authorize_airtable(user_id, org_id):
    # Step 1: Create security state token
    state_data = {
        'state': secrets.token_urlsafe(32),  # Random secure string
        'user_id': user_id,                  # Who is connecting
        'org_id': org_id                     # Which organization
    }
    
    # Step 2: Encode state data for URL transmission
    encoded_state = base64.urlsafe_b64encode(json.dumps(state_data).encode('utf-8')).decode('utf-8')
    
    # Step 3: Generate PKCE challenge (enhanced security)
    code_verifier = secrets.token_urlsafe(32)  # Secret we keep
    m = hashlib.sha256()
    m.update(code_verifier.encode('utf-8'))
    code_challenge = base64.urlsafe_b64encode(m.digest()).decode('utf-8').replace('=', '')  # Public challenge
    
    # Step 4: Build Airtable authorization URL with PKCE
    auth_url = f'https://airtable.com/oauth2/v1/authorize?client_id={CLIENT_ID}&response_type=code&owner=user&redirect_uri={REDIRECT_URI}&state={encoded_state}&code_challenge={code_challenge}&code_challenge_method=S256&scope={scope}'
    
    # Step 5: Store state and verifier in Redis (10 minutes expiry)
    await add_key_value_redis(f'airtable_state:{org_id}:{user_id}', json.dumps(state_data), expire=600)
    await add_key_value_redis(f'airtable_verifier:{org_id}:{user_id}', code_verifier, expire=600)
    
    return auth_url  # Send URL back to frontend
```

**PKCE Security Explained**:
- **Code Verifier**: Random secret we generate and store
- **Code Challenge**: SHA256 hash of the verifier (sent to Airtable)
- **Verification**: Airtable verifies we have the original verifier
- **Benefit**: Prevents authorization code interception attacks

**Airtable Scopes Used**:
```
data.records:read data.records:write data.recordComments:read data.recordComments:write schema.bases:read schema.bases:write
```

**Frontend Action**:
```javascript
// Opens Airtable authorization in popup window
const newWindow = window.open(authURL, 'Airtable Authorization', 'width=600, height=600');
```

---

### 2. `/integrations/airtable/oauth2callback` - HANDLE THE RESPONSE

**What it does**: Processes Airtable's response and exchanges code for access token

**Step-by-Step Process**:

```python
async def oauth2callback_airtable(request: Request):
    # Step 1: Extract authorization code and state from URL
    code = request.query_params.get('code')           # Authorization code from Airtable
    encoded_state = request.query_params.get('state') # Security token we sent
    
    # Step 2: Decode and verify the state (security check)
    state_data = json.loads(base64.urlsafe_b64decode(encoded_state).decode('utf-8'))
    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')
    
    # Step 3: Verify state matches what we stored
    saved_state = await get_value_redis(f'airtable_state:{org_id}:{user_id}')
    if original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='Security check failed!')
    
    # Step 4: Retrieve the code verifier for PKCE
    code_verifier = await get_value_redis(f'airtable_verifier:{org_id}:{user_id}')
    
    # Step 5: Exchange authorization code for access token with PKCE
    # Note: Airtable uses Basic Auth with base64 encoded client_id:client_secret
    encoded_client_id_secret = base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()
    
    token_response = await httpx.post('https://airtable.com/oauth2/v1/token',
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI,
            'client_id': CLIENT_ID,
            'code_verifier': code_verifier.decode('utf-8'),  # PKCE verification
        },
        headers={
            'Authorization': f'Basic {encoded_client_id_secret}',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
    )
    
    # Step 6: Store access token temporarily in Redis
    await add_key_value_redis(f'airtable_credentials:{org_id}:{user_id}', json.dumps(token_response.json()), expire=600)
    
    # Step 7: Clean up state and verifier
    await delete_key_redis(f'airtable_state:{org_id}:{user_id}')
    await delete_key_redis(f'airtable_verifier:{org_id}:{user_id}')
    
    # Step 8: Close the popup window
    return HTMLResponse(content="<script>window.close();</script>")
```

**What Airtable token response contains**:
```json
{
  "access_token": "patXXXXXXXXXXXXXX",
  "token_type": "Bearer",
  "expires_in": 7200,
  "refresh_token": "rtXXXXXXXXXXXXXX",
  "scope": "data.records:read data.records:write schema.bases:read"
}
```

---

### 3. `/integrations/airtable/credentials` - GET THE ACCESS TOKEN

**What it does**: Frontend retrieves stored credentials after OAuth completion

**Step-by-Step Process**:

```python
async def get_airtable_credentials(user_id, org_id):
    # Step 1: Retrieve credentials from Redis
    credentials = await get_value_redis(f'airtable_credentials:{org_id}:{user_id}')
    
    # Step 2: Check if credentials exist
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    
    # Step 3: Parse JSON data
    credentials = json.loads(credentials)
    
    # Step 4: Clean up - delete from Redis (one-time use)
    await delete_key_redis(f'airtable_credentials:{org_id}:{user_id}')
    
    # Step 5: Return credentials to frontend
    return credentials
```

---

### 4. `/integrations/airtable/load` - FETCH THE ACTUAL DATA

**What it does**: Uses access token to fetch bases and their tables in hierarchical structure

**Step-by-Step Process**:

```python
async def get_items_airtable(credentials):
    # Step 1: Extract access token from credentials
    credentials = json.loads(credentials)
    access_token = credentials.get('access_token')
    
    # Step 2: Fetch all bases with pagination support
    url = 'https://api.airtable.com/v0/meta/bases'
    list_of_responses = []
    fetch_items(access_token, url, list_of_responses)  # Handles pagination
    
    # Step 3: Process each base and its tables
    list_of_integration_items = []
    for base_response in list_of_responses:
        # Create base item
        base_item = create_integration_item_metadata_object(base_response, 'Base')
        list_of_integration_items.append(base_item)
        
        # Step 4: For each base, fetch its tables
        tables_url = f'https://api.airtable.com/v0/meta/bases/{base_response.get("id")}/tables'
        tables_response = requests.get(tables_url, headers={'Authorization': f'Bearer {access_token}'})
        
        if tables_response.status_code == 200:
            tables_data = tables_response.json()
            for table in tables_data['tables']:
                # Create table item with parent reference
                table_item = create_integration_item_metadata_object(
                    table, 'Table', 
                    parent_id=base_response.get('id'),
                    parent_name=base_response.get('name')
                )
                list_of_integration_items.append(table_item)
    
    return list_of_integration_items
```

**The `fetch_items` pagination function**:
```python
def fetch_items(access_token: str, url: str, aggregated_response: list, offset=None):
    """Handles Airtable pagination for bases"""
    params = {'offset': offset} if offset is not None else {}
    headers = {'Authorization': f'Bearer {access_token}'}
    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        results = response.json().get('bases', {})
        offset = response.json().get('offset', None)

        # Add results to our aggregated list
        for item in results:
            aggregated_response.append(item)

        # If there's more data, fetch it recursively
        if offset is not None:
            fetch_items(access_token, url, aggregated_response, offset)
```

**The `create_integration_item_metadata_object` function**:
```python
def create_integration_item_metadata_object(
    response_json: dict, item_type: str, parent_id=None, parent_name=None
):
    # Create parent reference for tables
    parent_id = None if parent_id is None else parent_id + '_Base'
    
    # Create standardized IntegrationItem
    return IntegrationItem(
        id=response_json.get('id') + '_' + item_type,  # Unique identifier
        name=response_json.get('name'),                # Display name
        type=item_type,                                # 'Base' or 'Table'
        parent_id=parent_id,                          # Parent reference
        parent_path_or_name=parent_name,              # Parent name for display
    )
```

---

## 🔍 Understanding Airtable Data Structure

### Airtable Hierarchy:
```
Workspace
├── Base 1 (like a database)
│   ├── Table A (like a spreadsheet)
│   ├── Table B
│   └── Table C
├── Base 2
│   ├── Table X
│   └── Table Y
```

### Sample Data Structure:
```json
[
    {
        "id": "appy2lIgkz7Ucsaq6_Base",
        "name": "Customer & Product Tracker",
        "type": "Base",
        "parent_id": null
    },
    {
        "id": "tblaKNYbjDrfNviEz_Table", 
        "name": "Customers",
        "type": "Table",
        "parent_id": "appy2lIgkz7Ucsaq6_Base",
        "parent_path_or_name": "Customer & Product Tracker"
    },
    {
        "id": "tblwlj9l1cX1anirY_Table",
        "name": "Products", 
        "type": "Table", 
        "parent_id": "appy2lIgkz7Ucsaq6_Base",
        "parent_path_or_name": "Customer & Product Tracker"
    }
]
```

---

## 🛡️ Security Features

### 1. **PKCE (Proof Key for Code Exchange)**
- **Enhanced Security**: Prevents authorization code interception
- **Code Verifier**: Random secret stored securely
- **Code Challenge**: SHA256 hash sent to Airtable
- **Verification**: Airtable verifies we have the original secret

### 2. **State Verification** (CSRF Protection)
- Random state token generated and verified
- Base64 encoded for URL safety

### 3. **Temporary Storage**
- Credentials expire after 10 minutes
- One-time use tokens
- Automatic cleanup of state and verifier

---

## 🔄 Complete Flow Summary

```
1. User clicks "Connect to Airtable"
   ↓
2. Frontend calls /authorize endpoint
   ↓  
3. Backend creates PKCE challenge and secure OAuth URL
   ↓
4. Frontend opens popup with OAuth URL
   ↓
5. User authorizes in Airtable
   ↓
6. Airtable redirects to /oauth2callback with code
   ↓
7. Backend verifies PKCE and exchanges code for token
   ↓
8. Backend stores token in Redis temporarily
   ↓
9. Frontend detects popup closed
   ↓
10. Frontend calls /credentials endpoint
    ↓
11. Backend returns stored credentials
    ↓
12. Frontend shows "AIRTABLE CONNECTED"
    ↓
13. User clicks "Load Data"
    ↓
14. Frontend calls /load endpoint with credentials
    ↓
15. Backend fetches bases with pagination
    ↓
16. Backend fetches tables for each base
    ↓
17. Backend converts data to IntegrationItem format
    ↓
18. Frontend displays hierarchical structure
```

---

## ✅ Key Features of Airtable Integration

### Advantages:
- **PKCE Security**: Most secure OAuth implementation
- **Hierarchical Data**: Maintains base-table relationships
- **Pagination Support**: Handles large numbers of bases
- **Rich Metadata**: Detailed base and table information

### Technical Highlights:
- **Two-Level API Calls**: First bases, then tables for each base
- **Parent-Child Relationships**: Tables reference their parent bases
- **Pagination Handling**: Recursive fetching for complete data
- **Security Best Practices**: PKCE + state verification

---

## 🚀 Key Differences from Other Integrations

### Airtable vs HubSpot:
- **Airtable**: PKCE for enhanced security
- **HubSpot**: Client secret for token exchange
- **Airtable**: Hierarchical base/table structure
- **HubSpot**: Flat CRM object structure

### Airtable vs Notion:
- **Airtable**: Two-step data fetching (bases → tables)
- **Notion**: Single search for all content
- **Airtable**: Structured database metaphor
- **Notion**: Flexible workspace metaphor

The Airtable integration demonstrates the most secure OAuth implementation with PKCE and shows how to handle hierarchical data structures efficiently!