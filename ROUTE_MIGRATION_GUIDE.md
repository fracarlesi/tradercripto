# Route Migration Guide: Sync → Async

**Purpose**: Guide for migrating existing synchronous API routes to async pattern.

**Status**: In Progress (T059-T061)

---

## Migration Pattern

### Before (Synchronous)
```python
from sqlalchemy.orm import Session
from database.connection import SessionLocal

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/items")
async def get_items(db: Session = Depends(get_db)):
    items = db.query(Item).all()  # ❌ Blocking query
    return items
```

### After (Asynchronous)
```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.database.connection import get_db

router = APIRouter()

@router.get("/items")
async def get_items(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Item))  # ✅ Non-blocking
    items = result.scalars().all()
    return items
```

---

## Step-by-Step Migration

### 1. Update Imports
```python
# Remove:
from sqlalchemy.orm import Session
from database.connection import SessionLocal

# Add:
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
from backend.database.connection import get_db
```

### 2. Remove Local get_db()
```python
# ❌ Remove this:
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ✅ Import from connection module instead
from backend.database.connection import get_db
```

### 3. Update Route Signatures
```python
# Before:
async def my_route(db: Session = Depends(get_db)):

# After:
async def my_route(db: AsyncSession = Depends(get_db)):
```

### 4. Convert Queries

#### SELECT (Read)
```python
# Before:
items = db.query(Item).filter(Item.active == True).all()
item = db.query(Item).filter(Item.id == item_id).first()

# After:
result = await db.execute(select(Item).where(Item.active == True))
items = result.scalars().all()

result = await db.execute(select(Item).where(Item.id == item_id))
item = result.scalar_one_or_none()
```

#### INSERT (Create)
```python
# Before:
item = Item(name="test")
db.add(item)
db.commit()
db.refresh(item)

# After:
item = Item(name="test")
db.add(item)
await db.flush()  # Commit handled by get_db()
await db.refresh(item)
```

#### UPDATE
```python
# Before:
item = db.query(Item).filter(Item.id == item_id).first()
item.name = "updated"
db.commit()

# After:
result = await db.execute(select(Item).where(Item.id == item_id))
item = result.scalar_one_or_none()
item.name = "updated"
await db.flush()
```

#### DELETE
```python
# Before:
db.query(Item).filter(Item.id == item_id).delete()
db.commit()

# After:
await db.execute(delete(Item).where(Item.id == item_id))
await db.flush()
```

#### COUNT
```python
# Before:
count = db.query(Item).count()

# After:
from sqlalchemy import func
result = await db.execute(select(func.count()).select_from(Item))
count = result.scalar()
```

### 5. Handle Relationships
```python
# Before:
user = db.query(User).filter(User.id == user_id).first()
accounts = user.accounts  # Lazy load

# After:
from sqlalchemy.orm import selectinload
result = await db.execute(
    select(User)
    .where(User.id == user_id)
    .options(selectinload(User.accounts))
)
user = result.scalar_one_or_none()
accounts = user.accounts if user else []
```

---

## Common Patterns

### Pattern 1: List with Filter
```python
@router.get("/accounts")
async def list_accounts(active_only: bool = True, db: AsyncSession = Depends(get_db)):
    stmt = select(Account)
    if active_only:
        stmt = stmt.where(Account.is_active == "true")

    result = await db.execute(stmt)
    accounts = result.scalars().all()
    return accounts
```

### Pattern 2: Get by ID
```python
@router.get("/accounts/{account_id}")
async def get_account(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Account).where(Account.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return account
```

### Pattern 3: Create with Validation
```python
from pydantic import BaseModel

class AccountCreate(BaseModel):
    name: str
    initial_capital: float

@router.post("/accounts")
async def create_account(
    data: AccountCreate,
    db: AsyncSession = Depends(get_db)
):
    account = Account(
        name=data.name,
        initial_capital=data.initial_capital,
        current_cash=data.initial_capital
    )
    db.add(account)
    await db.flush()
    await db.refresh(account)
    return account
```

### Pattern 4: Update
```python
class AccountUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None

@router.put("/accounts/{account_id}")
async def update_account(
    account_id: int,
    data: AccountUpdate,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Account).where(Account.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if data.name is not None:
        account.name = data.name
    if data.is_active is not None:
        account.is_active = "true" if data.is_active else "false"

    await db.flush()
    await db.refresh(account)
    return account
```

### Pattern 5: Complex Joins
```python
@router.get("/accounts/{account_id}/with-positions")
async def get_account_with_positions(
    account_id: int,
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(Account)
        .where(Account.id == account_id)
        .options(selectinload(Account.positions))
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return {
        "account": account,
        "positions": account.positions
    }
```

---

## Migration Checklist

For each route file:

- [ ] Update imports (AsyncSession, select, backend.database.connection.get_db)
- [ ] Remove local get_db() function
- [ ] Update all route signatures (Session → AsyncSession)
- [ ] Convert db.query() to await db.execute(select())
- [ ] Convert .filter() to .where()
- [ ] Convert .first() to .scalar_one_or_none()
- [ ] Convert .all() to .scalars().all()
- [ ] Convert .count() to func.count()
- [ ] Remove explicit db.commit() calls (handled by get_db())
- [ ] Add await to db.flush() and db.refresh()
- [ ] Test with concurrent requests

---

## Files to Migrate

### Priority 1 (T059-T061)
- [ ] `backend/api/account_routes.py` (T059)
  - GET /api/account/list
  - GET /api/account/{account_id}/overview
  - POST /api/account/create
  - PUT /api/account/{account_id}/update

- [ ] `backend/api/market_data_routes.py` (T060)
  - GET /api/market/prices
  - GET /api/market/klines

- [ ] `backend/api/order_routes.py` (T061)
  - GET /api/orders
  - POST /api/orders
  - GET /api/orders/{id}

### Priority 2 (Future)
- [ ] `backend/api/config_routes.py`
- [ ] `backend/api/crypto_routes.py`
- [ ] `backend/api/ranking_routes.py`
- [ ] `backend/api/user_routes.py`
- [ ] `backend/api/account_management_routes.py`

---

## Testing

After migration, test each route:

```bash
# 1. Single request
curl http://localhost:5611/api/accounts

# 2. Concurrent requests (test non-blocking)
for i in {1..10}; do
  curl http://localhost:5611/api/accounts &
done
wait

# 3. Load test with hey
hey -n 100 -c 10 http://localhost:5611/api/accounts
```

Verify:
- ✅ p95 latency <200ms
- ✅ No blocking (all 10 concurrent requests complete)
- ✅ No database deadlocks
- ✅ Pool connections released properly

---

## Common Issues

### Issue 1: "Task attached to different loop"
**Cause**: Mixing sync and async code
**Solution**: Ensure all database operations use `await`

### Issue 2: "Object is not bound to a Session"
**Cause**: Accessing lazy-loaded relationships after session closed
**Solution**: Use `selectinload()` or `joinedload()` to eager load

### Issue 3: "Pool exhausted"
**Cause**: Not releasing connections (missing await)
**Solution**: Check all queries have `await`, connections auto-release via get_db()

### Issue 4: "Cannot use query with AsyncSession"
**Cause**: Using old sync API (`db.query()`)
**Solution**: Replace with `await db.execute(select())`

---

## Performance Expectations

After migration:
- **Latency**: p50 <50ms, p95 <200ms, p99 <500ms
- **Throughput**: 100+ req/s with 10 concurrent connections
- **Pool usage**: <50% under normal load
- **CPU**: Non-blocking, low CPU during I/O

---

## Reference

- New async routes: `backend/api/health_routes.py`, `backend/api/sync_routes.py`
- Repository examples: All files in `backend/repositories/`
- SQLAlchemy 2.0 docs: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
