import os
import re
import subprocess
import logging
import shutil
from pathlib import Path
from datetime import datetime

def setup_logging(log_file_path):
    """Set up logging configuration"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file_path)
        ]
    )

def parse_chunks(log_file_path):
    """Parse the log file and extract chunk information"""
    chunks = []
    current_chunk = None
    
    with open(log_file_path, 'r') as f:
        for line in f:
            # Check for chunk header
            chunk_match = re.match(r'^Chunk #(\d+) \((\d+) files, ([\d.]+)MB\):', line.strip())
            if chunk_match:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = {
                    'number': int(chunk_match.group(1)),
                    'file_count': int(chunk_match.group(2)),
                    'size_mb': float(chunk_match.group(3)),
                    'files': []
                }
            elif current_chunk and line.strip().startswith('- '):
                # Extract file path from line
                file_path = line.strip().split(' ')[1]
                current_chunk['files'].append(file_path)
        
        # Add the last chunk if exists
        if current_chunk:
            chunks.append(current_chunk)
    
    return chunks

def git_add_files(files):
    """Git add all files in the list"""
    try:
        for file in files:
            # Check if file exists before adding
            if os.path.exists(file):
                subprocess.run(['git', 'add', file], check=True)
                logging.info(f"Added to staging: {file}")
            else:
                logging.warning(f"File not found, skipping: {file}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to add files to git: {e}")
        return False
    except Exception as e:
        logging.error(f"Error during git add: {e}")
        return False

def git_commit_chunk(chunk_number, file_count):
    """Create a git commit for the chunk"""
    try:
        commit_message = f"Chunk #{chunk_number} - {file_count} files pushed successfully"
        subprocess.run(['git', 'commit', '-m', commit_message], check=True)
        logging.info(f"Committed chunk #{chunk_number} with message: '{commit_message}'")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to commit chunk #{chunk_number}: {e}")
        return False

def git_push():
    """Push changes to remote"""
    try:
        subprocess.run(['git', 'push'], check=True)
        logging.info("Pushed changes to remote repository")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to push changes: {e}")
        return False

def load_processed_chunks(processed_chunks_file):
    """Load the list of already processed chunk numbers"""
    if os.path.exists(processed_chunks_file):
        with open(processed_chunks_file, 'r') as f:
            return {int(line.strip()) for line in f}
    return set()

def save_processed_chunk(processed_chunks_file, chunk_number):
    """Save the processed chunk number to a file"""
    with open(processed_chunks_file, 'a') as f:
        f.write(f"{chunk_number}\n")

def copy_log_file(log_file, target_dir):
    """Copy the provided log file to a new directory with a timestamp"""
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
    
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    destination = os.path.join(target_dir, f"log_{timestamp}.log")
    
    try:
        shutil.copy(log_file, destination)
        logging.info(f"Copied log file to: {destination}")
        return destination
    except Exception as e:
        logging.error(f"Error copying log file: {e}")
        return None

def process_chunks(chunks, processed_chunks_file):
    """Process all chunks and perform git operations"""
    total_chunks = len(chunks)
    processed_chunks = load_processed_chunks(processed_chunks_file)
    logging.info(f"Starting to process {total_chunks} chunks...")

    for chunk in chunks:
        chunk_num = chunk['number']
        if chunk_num in processed_chunks:
            logging.info(f"Chunk #{chunk_num} has already been processed, skipping.")
            continue

        file_count = chunk['file_count']
        files = chunk['files']
        
        logging.info(f"\nProcessing Chunk #{chunk_num} ({file_count} files, {chunk['size_mb']}MB)")

        # Git add files
        if not git_add_files(files):
            logging.error(f"Skipping Chunk #{chunk_num} due to git add failure")
            continue
        
        # Git commit
        if not git_commit_chunk(chunk_num, file_count):
            logging.error(f"Skipping Chunk #{chunk_num} due to commit failure")
            continue
        
        # Git push (optional: could push after each chunk or at the end)
        if not git_push():
            logging.error(f"Push failed after Chunk #{chunk_num}")
            # Continue with next chunk even if push fails
            continue
        
        save_processed_chunk(processed_chunks_file, chunk_num)
        logging.info(f"Successfully processed Chunk #{chunk_num}")
    
    logging.info("\nAll chunks processed!")

def main():
    log_file = input("Enter the path to the .log file: ").strip()
    processed_chunks_file = "processed_chunks.txt"
    logs_dir = "logs"
    
    # Set up the log file for the script execution
    timestamped_log_file = f"process_chunks_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    setup_logging(timestamped_log_file)
    
    print(f"Log file path: {log_file}")
    
    # Validate paths
    if not os.path.isfile(log_file):
        logging.error("Log file not found!")
        return
    
    # Copy the log file to the logs directory
    copied_log_file = copy_log_file(log_file, logs_dir)
    if not copied_log_file:
        logging.error("Failed to copy the log file, aborting.")
        return
    
    try:
        # Parse the log file
        logging.info(f"Parsing log file: {copied_log_file}")
        chunks = parse_chunks(copied_log_file)
        
        if not chunks:
            logging.warning("No chunks found in the log file!")
            return
        
        logging.info(f"Found {len(chunks)} chunks to process")
        
        # Process chunks
        process_chunks(chunks, processed_chunks_file)
        
    except Exception as e:
        logging.error(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
