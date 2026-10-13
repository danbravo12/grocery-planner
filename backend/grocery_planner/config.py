from grocery_planner.domain import Store

# Stores the app compares. Code must use this, never iterate the Store enum: the enum may
# list stores that have no seed data yet (currently Aldi).
ACTIVE_STORES: tuple[Store, ...] = (Store.COLES, Store.WOOLWORTHS)
