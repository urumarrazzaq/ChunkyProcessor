import os
import re
import subprocess
from pathlib import Path
import logging
from datetime import datetime

def setup_logging():
    """Set up logging configuration"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
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

def git_add_files(files, repo_path):
    """Git add all files in the list"""
    try:
        # Change to repo directory
        os.chdir(repo_path)
        
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

def process_chunks(chunks, repo_path):
    """Process all chunks and perform git operations"""
    total_chunks = len(chunks)
    logging.info(f"Starting to process {total_chunks} chunks...")
    
    for chunk in chunks:
        chunk_num = chunk['number']
        file_count = chunk['file_count']
        files = chunk['files']
        
        logging.info(f"\nProcessing Chunk #{chunk_num} ({file_count} files, {chunk['size_mb']}MB)")
        
        # Git add files
        if not git_add_files(files, repo_path):
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
        
        logging.info(f"Successfully processed Chunk #{chunk_num}")
    
    logging.info("\nAll chunks processed!")

def main():
    setup_logging()
    
    # Get input from user
    log_file_path = input("Enter the path to the .log file: ").strip()
    repo_path = input("Enter the path to the Git repository: ").strip()
    
    print(f"Log file path: {log_file_path}")
    print(f"Repository path: {repo_path}")
    
    # Validate paths
    if not os.path.isfile(log_file_path):
        logging.error("Log file not found!")
        return
    
    if not os.path.isdir(repo_path):
        logging.error("Repository directory not found!")
        return
    
    if not os.path.isdir(os.path.join(repo_path, '.git')):
        logging.error("The specified directory is not a Git repository!")
        return
    
    try:
        # Parse the log file
        logging.info(f"Parsing log file: {log_file_path}")
        chunks = parse_chunks(log_file_path)
        
        if not chunks:
            logging.warning("No chunks found in the log file!")
            return
        
        logging.info(f"Found {len(chunks)} chunks to process")
        
        # Process chunks
        process_chunks(chunks, repo_path)
        
    except Exception as e:
        logging.error(f"An error occurred: {e}")

if __name__ == "__main__":
    main()