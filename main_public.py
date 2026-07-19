from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import random
from datetime import datetime, timedelta

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"message": "AWS Dashboard backend is running (demo mode)"}

@app.get("/buckets")
def list_buckets():
    bucket_names = ["user-uploads", "backup-data", "static-website", "logs-archive", "my-first-bucket"]
    return {"buckets": bucket_names}

@app.get("/instances")
def list_instances():
    types = ["t2.micro", "t3.small", "t2.medium"]
    states = ["running", "running", "running", "stopped"]
    instances = []
    for i in range(5):
        instances.append({
            "id": f"i-{random.randint(10**11, 10**12-1):x}",
            "type": random.choice(types),
            "state": random.choice(states)
        })
    return {"instances": instances}

@app.get("/billing")
def get_billing():
    services = ["EC2", "S3", "Lambda", "RDS", "CloudWatch"]
    breakdown = []
    for service in services:
        cost = round(random.uniform(5000, 50000), 0)
        trend = round(random.uniform(-40, 60), 0)
        breakdown.append({
            "invoice_id": random.randint(100000, 999999),
            "service": service,
            "usage_hours": random.randint(500, 10000),
            "interval": "Oct 1 - Oct 31",
            "cost": cost,
            "trend": trend,
            "projected_cost": round(cost * random.uniform(1.1, 1.5), 0)
        })
    total = round(sum(item["cost"] for item in breakdown), 2)
    return {
        "total_cost": total,
        "currency": "USD",
        "breakdown": breakdown
    }

@app.get("/billing/trend")
def get_billing_trend():
    today = datetime.now()
    trend = []
    base_cost = random.uniform(800, 1200)
    for i in range(30, 0, -1):
        day = today - timedelta(days=i)
        base_cost += random.uniform(-100, 150)
        base_cost = max(base_cost, 200)
        trend.append({
            "date": day.strftime("%b %d"),
            "cost": round(base_cost, 2)
        })
    return {"trend": trend}