import os
import tkinter as tk
from tkinter import filedialog
import subprocess
import tifffile
import imagecodecs

input_folder = filedialog.askdirectory(title="Select folder")
output_folder = input_folder

# List all files in the input folder
input_files = os.listdir(input_folder)

for file_name in input_files:
    if file_name.lower().endswith(".tif") or file_name.lower().endswith(".tiff"):
        input_path = os.path.join(input_folder, file_name)
        output_path = os.path.join(output_folder, file_name)

        try:
            # Read the 32-bit TIFF
            tiff_data = tifffile.imread(input_path)

            # Normalize the data to 16-bit range
            tiff_data_normalized = (tiff_data - tiff_data.min()) / (tiff_data.max() - tiff_data.min())
            tiff_data_16bit = (tiff_data_normalized * 65535).astype('uint16')

            # Write the 16-bit TIFF
            #tifffile.imwrite(output_path, tiff_data_16bit, dtype='uint16')
            tifffile.imwrite(output_path, tiff_data_16bit, dtype='uint16', compression ='zlib')

            print(f"Converted and saved {file_name}")
        except Exception as e:
            print(f"Error processing {file_name}: {e}")
