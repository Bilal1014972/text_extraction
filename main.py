from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from extract import router as extract_router
from summarize import router as summarize_router



app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


################ Extract Endpoint ################
app.include_router(extract_router, tags=["Extract Endpoint"])

############## Summarize ENDPOINT #################
app.include_router(summarize_router, tags=["Summarize Endpoint"])
