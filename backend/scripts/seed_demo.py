from backend.app.database import Base, SessionLocal, engine
from backend.app.seed import seed_demo_data

Base.metadata.create_all(bind=engine)
with SessionLocal() as session:
    seed_demo_data(session)
print("Fictional demo rows inserted where tables were empty; existing records were preserved.")
