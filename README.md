# Shift Planner

Driver-less profit planner for trucking companies: enter the day's loads and the driver slots you can field per yard,
and it builds the most profitable set of shifts (OR-Tools routing engine), with fuel, surcharge, driver pay,
sub-hauler shares, hours-of-service caps and site windows all accounted for.

Run locally: `pip install -r requirements.txt && python main.py` (SQLite in data/planner.db).
Deploy: Render Blueprint (render.yaml). Environment: PLANNER_PASSWORD, GOOGLE_MAPS_API_KEY (Routes API),
GOOGLE_MAPS_BROWSER_KEY (Maps JavaScript + Directions, restricted to the site), optional COMPANY_NAME, PRODUCT_NAME,
SEED_PETROL=1 to load the Petrol Transport starter data on first start.
