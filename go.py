import tkinter as tk
from tkinter import filedialog
import os
import subprocess
import time
from datetime import timedelta

root = tk.Tk()
root.withdraw()

process_path = "videoFacesToUVNDC.py"

video_paths = filedialog.askopenfilenames(title="Select video files", filetypes=[("Video files", "*.mov")])
output_path = filedialog.askdirectory(title="Select output folder")


start_time = time.time() # Start time

for video_path in video_paths:
    filename = os.path.splitext(os.path.basename(video_path))[0]
    cmd = ["python", process_path, "--input_video", video_path, "--tmp_output_folder", output_path]
    print("command line is : ", f"python {process_path} --input_video {video_path} --tmp_output_folder {output_path}")
    subprocess.run(cmd)

end_time = time.time() # End time
elapsed_time = end_time - start_time # Calculate the elapsed time
formatted_time = str(timedelta(seconds=elapsed_time)).split(".")[0] # Format the elapsed time
print()
print(f"Elapsed time: {formatted_time}") # Print the elapsed time

input("Please press Enter to exit...")