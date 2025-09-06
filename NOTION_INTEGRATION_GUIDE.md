# Notion Integration - Complete Technical Guide

## 🎯 Overview
This guide explains how the Notion integration works in our application. Notion is a workspace platform that combines notes, databases, wikis, and project management. Our integration allows users to securely connect their Notion workspace and fetch their pages and databases.

## 🏗️ Architecture Overview

```
Frontend (React) ↔ Backend (FastAPI) ↔ Redis (Storage) ↔ Notion API
```

**Data Flow**: 
- **Frontend**: React components handle user interactions
- **Backend**: FastAPI server manages OAuth flow and API calls
- **Redis**: Temporary storage for OAuth tokens and state
- **Notion API**: External workspace service providing content data

## 🔄 Complete Flow Explanation

### The Big Picture:
1. User clicks "Connect to Notion" → OAuth authorization starts
2. User authorizes in Notion → We receive authorization code
3. Backend exchanges code for access token → Credentials stored temporarily
4. User clicks "Load Data" → We search and fetch workspace content
5. Data is displayed → User sees their pages, databases, and content

---

## 📋 The 4 Notion Endpoints Explained

### 1. `/integrations/notion/authorize` - START THE CONNECTION

**What it does**: Creates a secure authorization URL for Notion OAuth

**Step-by-Step Process**:

```python
async def authorize_notion(user_id, org_id):
    # Step 1: Create security state token
    state_data = {
        'state': secrets.token_urlsafe(32),  # Random secure string
        'user_id': user_id,                  # Who is connecting
        'org_id': org_id                     # Which organization
    }
    
    # Step 2: Store state in Redis for verification (10 minutes expiry)
    encoded_state = json.dumps(state_data)
    await add_key_value_redis(f'notion_state:{org_id}:{user_id}', encoded_state, expire=600)
    
    # Step 3: Build Notion authorization URL
    auth_url = f'https://api.notion.com/v1/oauth/authorize?client_id={CLIENT_ID}&response_type=code&owner=user&redirect_uri={REDIRECT_URI}&state={encoded_state}'
    
    return auth_url  # Send URL back to frontend
```

**Key Features**:
- **Simple State Management**: Direct JSON encoding (no base64)
- **Owner Parameter**: Set to 'user' for personal workspace access
- **Redirect URI**: Must match exactly with Notion app settings

**Frontend Action**:
```javascript
// Opens Notion authorization in popup window
const newWindow = window.open(authURL, 'Notion Authorization', 'width=600, height=600');
```

---

### 2. `/integrations/notion/oauth2callback` - HANDLE THE RESPONSE

**What it does**: Processes Notion's response after user authorization

**Step-by-Step Process**:

```python
async def oauth2callback_notion(request: Request):
    # Step 1: Extract authorization code and state from URL
    code = request.query_params.get('code')           # Authorization code from Notion
    encoded_state = request.query_params.get('state') # Security token we sent
    
    # Step 2: Decode and verify the state (security check)
    state_data = json.loads(encoded_state)
    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')
    
    # Step 3: Verify state matches what we stored
    saved_state = await get_value_redis(f'notion_state:{org_id}:{user_id}')
    if original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='Security check failed!')
    
    # Step 4: Exchange authorization code for access token
    # Note: Notion uses Basic Auth with base64 encoded client_id:client_secret
    encoded_client_id_secret = base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()
    
    token_response = await httpx.post('https://api.notion.com/v1/oauth/token', 
        json={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI
        },
        headers={
            'Authorization': f'Basic {encoded_client_id_secret}',
            'Content-Type': 'application/json',
        }
    )
    
    # Step 5: Store access token temporarily in Redis
    await add_key_value_redis(f'notion_credentials:{org_id}:{user_id}', json.dumps(token_response.json()), expire=600)
    
    # Step 6: Close the popup window
    return HTMLResponse(content="<script>window.close();</script>")
```

**Notion-Specific Features**:
- **JSON Request Body**: Unlike form data, Notion expects JSON
- **Basic Authentication**: Uses base64 encoded client credentials
- **Workspace Access**: Token provides access to user's workspace

---

### 3. `/integrations/notion/credentials` - GET THE ACCESS TOKEN

**What it does**: Frontend retrieves stored credentials after OAuth completion

**Step-by-Step Process**:

```python
async def get_notion_credentials(user_id, org_id):
    # Step 1: Retrieve credentials from Redis
    credentials = await get_value_redis(f'notion_credentials:{org_id}:{user_id}')
    
    # Step 2: Check if credentials exist
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    
    # Step 3: Parse JSON data
    credentials = json.loads(credentials)
    
    # Step 4: Clean up - delete from Redis (one-time use)
    await delete_key_redis(f'notion_credentials:{org_id}:{user_id}')
    
    # Step 5: Return credentials to frontend
    return credentials
```

**What Notion credentials contain**:
```json
{
  "access_token": "secret_...",
  "token_type": "bearer",
  "bot_id": "...",
  "workspace_name": "User's Workspace",
  "workspace_icon": "...",
  "workspace_id": "..."
}
```

---

### 4. `/integrations/notion/load` - FETCH THE ACTUAL DATA

**What it does**: Uses access token to search and fetch workspace content

**Step-by-Step Process**:

```python
async def get_items_notion(credentials):
    # Step 1: Extract access token from credentials
    credentials = json.loads(credentials)
    access_token = credentials.get('access_token')
    
    # Step 2: Use Notion Search API to find all accessible content
    response = requests.post('https://api.notion.com/v1/search',
        headers={
            'Authorization': f'Bearer {access_token}',
            'Notion-Version': '2022-06-28',  # Required API version
        }
    )
    
    # Step 3: Process search results
    if response.status_code == 200:
        results = response.json()['results']
        list_of_integration_items = []
        
        # Step 4: Convert each result to IntegrationItem format
        for result in results:
            integration_item = create_integration_item_metadata_object(result)
            list_of_integration_items.append(integration_item)
        
        return list_of_integration_items
```

**The `create_integration_item_metadata_object` function**:
```python
def create_integration_item_metadata_object(response_json: dict):
    # Step 1: Extract name from nested properties structure
    name = _recursive_dict_search(response_json['properties'], 'content')
    
    # Step 2: Determine parent information
    parent_type = response_json['parent']['type'] if response_json['parent']['type'] else ''
    
    if response_json['parent']['type'] == 'workspace':
        parent_id = None  # Top-level item
    else:
        parent_id = response_json['parent'][parent_type]
    
    # Step 3: Handle various name formats
    name = _recursive_dict_search(response_json, 'content') if name is None else name
    name = 'multi_select' if name is None else name
    name = response_json['object'] + ' ' + name  # Prefix with object type
    
    # Step 4: Create standardized IntegrationItem
    return IntegrationItem(
        id=response_json['id'],
        type=response_json['object'],  # 'page' or 'database'
        name=name,
        creation_time=response_json['created_time'],
        last_modified_time=response_json['last_edited_time'],
        parent_id=parent_id,
    )
```

**The `_recursive_dict_search` helper function**:
```python
def _recursive_dict_search(data, target_key):
    """Recursively search for a key in nested dictionaries"""
    if target_key in data:
        return data[target_key]
    
    for value in data.values():
        if isinstance(value, dict):
            result = _recursive_dict_search(value, target_key)
            if result is not None:
                return result
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    result = _recursive_dict_search(item, target_key)
                    if result is not None:
                        return result
    return None
```

---

## 🔍 Understanding Notion Data Structure

### Notion Object Types:
- **Pages**: Individual pages with content
- **Databases**: Structured collections of pages
- **Blocks**: Content within pages (text, images, etc.)

### Notion Properties Structure:
Notion properties are deeply nested and vary by content type:
```json
{
  "properties": {
    "title": {
      "title": [
        {
          "text": {
            "content": "Page Title"
          }
        }
      ]
    }
  }
}
```

### Parent-Child Relationships:
- **Workspace**: Top-level container
- **Page**: Can contain other pages or databases
- **Database**: Contains database entries (pages)

---

## 🛡️ Security Features

### 1. **State Verification** (CSRF Protection)
- Random state token generated and verified
- Prevents unauthorized access attempts

### 2. **Temporary Storage**
- Credentials expire after 10 minutes
- One-time use tokens

### 3. **API Versioning**
- Specific Notion API version required
- Ensures consistent behavior

---

## 🔄 Complete Flow Summary

```
1. User clicks "Connect to Notion"
   ↓
2. Frontend calls /authorize endpoint
   ↓  
3. Backend creates secure OAuth URL
   ↓
4. Frontend opens popup with OAuth URL
   ↓
5. User authorizes workspace access in Notion
   ↓
6. Notion redirects to /oauth2callback
   ↓
7. Backend exchanges code for access token
   ↓
8. Backend stores token in Redis temporarily
   ↓
9. Frontend detects popup closed
   ↓
10. Frontend calls /credentials endpoint
    ↓
11. Backend returns stored credentials
    ↓
12. Frontend shows "NOTION CONNECTED"
    ↓
13. User clicks "Load Data"
    ↓
14. Frontend calls /load endpoint with credentials
    ↓
15. Backend searches Notion workspace
    ↓
16. Backend converts data to IntegrationItem format
    ↓
17. Frontend displays workspace content
```

---

## ✅ Key Features of Notion Integration

### Advantages:
- **Universal Search**: Single API call gets all accessible content
- **Rich Metadata**: Detailed creation and modification times
- **Hierarchical Structure**: Parent-child relationships preserved
- **Flexible Content**: Handles various page and database types

### Challenges:
- **Complex Properties**: Nested structure requires recursive parsing
- **Variable Names**: Content titles can be in different property formats
- **API Versioning**: Requires specific version header

---

## 🚀 Key Differences from Other Integrations

### Notion vs HubSpot:
- **Notion**: Single search endpoint for all content
- **HubSpot**: Multiple endpoints for different object types
- **Notion**: Flexible workspace structure
- **HubSpot**: Structured CRM data

### Notion vs Airtable:
- **Notion**: Search-based content discovery
- **Airtable**: Hierarchical base/table structure
- **Notion**: Complex nested properties
- **Airtable**: Simpler metadata structure

The Notion integration demonstrates how to handle flexible, search-based content platforms with complex data structures!