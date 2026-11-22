import os
import spaces
import torch
import gradio as gr
import uvicorn
from server import app as fastapi_app

# ZeroGPU initialization
try:
    zero = torch.Tensor([0]).cuda()
    print("ZeroGPU device initialized:", zero.device)
except Exception as e:
    print("ZeroGPU note:", e)

@spaces.GPU
def gpu_health_check(query: str = ""):
    z = torch.Tensor([0]).cuda()
    return f"ZeroGPU Device: {z.device}"

with gr.Blocks(title="Manipur Voter Search Portal") as demo:
    # Hidden components to register @spaces.GPU with Gradio for ZeroGPU supervisor
    gpu_in = gr.Textbox(visible=False)
    gpu_out = gr.Textbox(visible=False)
    gpu_btn = gr.Button("GPU Helper", visible=False)
    gpu_btn.click(fn=gpu_health_check, inputs=gpu_in, outputs=gpu_out)

    gr.HTML("""
        <style>
            body, html, .gradio-container {
                margin: 0 !important;
                padding: 0 !important;
                width: 100vw !important;
                height: 100vh !important;
                max-width: 100vw !important;
                overflow: hidden !important;
            }
            footer { display: none !important; }
        </style>
        <iframe src="/portal" style="position:fixed; top:0; left:0; width:100vw; height:100vh; border:none; margin:0; padding:0; z-index:999999;"></iframe>
    """)

# Disable ssr_mode so no background Node.js proxy attempts to grab port 7860
app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio", ssr_mode=False)

if __name__ == "__main__":
    print("Starting Uvicorn on 0.0.0.0:7860...")
    uvicorn.run(app, host="0.0.0.0", port=7860)
