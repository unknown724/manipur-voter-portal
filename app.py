import gradio as gr
from server import app as fastapi_app

# Mount our custom FastAPI portal into Gradio so all existing routes (/ and /api/...) are served seamlessly
demo = gr.Blocks(title="Manipur Voter Portal")
with demo:
    gr.HTML("<meta http-equiv='refresh' content='0; url=/'>")

app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(fastapi_app, host="0.0.0.0", port=7860)
