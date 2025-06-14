"""
Job search routes for the SeekrAI platform.

This module defines a Flask Blueprint (`job_bp`) exposing endpoints for initiating job
searches (`/search_jobs`) and retrieving search progress (`/job_progress/<job_id>`).
It supports both synchronous and asynchronous searches, with optional analysis via
`ResumeProcessor`. Progress is stored in Redis when available (with automatic
fallback to in-memory storage) and includes phases: initializing, scraping,
analysis, and completion. Utility functions handle sanitization for JSON and
filename generation.
"""

import os
import csv
import json
import uuid
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from jobspy import scrape_jobs
from resume_processor import ResumeProcessor
from config_loader import get_config
import pandas as pd
import logging
import unicodedata
import re

# Try to import Redis, fall back to in-memory storage if not available
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Create blueprint
job_bp = Blueprint('jobs', __name__)

# Global progress storage (fallback for development)
job_progress_storage = {}
progress_lock = threading.Lock()

def get_redis_client():
    """Return a live Redis client or None if unavailable.

    Reads `cache.redis_url` from config (defaults to redis://localhost:6379/0),
    tries to connect and ping. On any error, logs a warning and returns None
    (so callers fall back to in-memory storage).

    Returns:
        redis.Redis or None: Connected Redis client, or None on failure.
    """
    try:
        config = get_config()
        redis_url = config.get('cache.redis_url', 'redis://localhost:6379/0')
        client = redis.from_url(redis_url, decode_responses=True)
        # Test connection
        client.ping()
        return client
    except Exception as e:
        logging.warning(f"Redis not available, falling back to in-memory storage: {e}")
        return None

def update_job_progress(job_id, phase, percent, details=None, analysis_progress=None):
    """Store progress for a job, preferring Redis but falling back to memory.

    Builds a dict with phase, percent, optional details and analysis_progress,
    plus a timestamp. If Redis is flagged and reachable, sets it with a 1h TTL;
    otherwise safely writes into a thread-locked dict.

    Args:
        job_id (str): Unique job UUID.
        phase (str): One of 'initializing', 'scraping', 'analyzing', 'complete', etc.
        percent (int): 0–100 progress percent.
        details (str, optional): Human-readable phase info.
        analysis_progress (dict, optional): Batch progress metadata.
    """
    progress_data = {
        'phase': phase,
        'percent': percent,
        'details': details,
        'analysis_progress': analysis_progress,
        'timestamp': datetime.now().isoformat()
    }
    
    # Try Redis first
    if REDIS_AVAILABLE:
        redis_client = get_redis_client()
        if redis_client:
            try:
                redis_client.setex(
                    f"job_progress:{job_id}", 
                    3600,  # Expire after 1 hour
                    json.dumps(progress_data)
                )
                return
            except Exception as e:
                logging.warning(f"Failed to store progress in Redis: {e}")
    
    # Fallback to in-memory storage
    with progress_lock:
        job_progress_storage[job_id] = progress_data

def get_job_progress(job_id):
    """Retrieve stored progress for a job, checking Redis first.

    If Redis is reachable, tries `get(job_progress:{job_id})` and JSON-loads it.
    On any failure or cache miss, reads from the in-memory dict under lock.

    Args:
        job_id (str): UUID returned when job was queued.

    Returns:
        dict: Progress dict with keys phase, percent, details, analysis_progress, timestamp;
              empty if not found.
    """
    # Try Redis first
    if REDIS_AVAILABLE:
        redis_client = get_redis_client()
        if redis_client:
            try:
                progress_data = redis_client.get(f"job_progress:{job_id}")
                if progress_data:
                    return json.loads(progress_data)
            except Exception as e:
                logging.warning(f"Failed to get progress from Redis: {e}")
    
    # Fallback to in-memory storage
    with progress_lock:
        return job_progress_storage.get(job_id, {})

def cleanup_job_progress(job_id):
    """Delete progress for a job from Redis (if possible) and memory.

    Ensures no leftover state for completed or cancelled jobs.

    Args:
        job_id (str): UUID of the job to remove.
    """
    # Try Redis first
    if REDIS_AVAILABLE:
        redis_client = get_redis_client()
        if redis_client:
            try:
                redis_client.delete(f"job_progress:{job_id}")
            except Exception as e:
                logging.warning(f"Failed to delete progress from Redis: {e}")
    
    # Also clean up in-memory storage
    with progress_lock:
        job_progress_storage.pop(job_id, None)

def sanitize_string_for_json(value):
    """Normalize and scrub a string for safe JSON embedding.

    - Unicode-normalizes to ASCII where possible.
    - Replaces quotes/backslashes/control chars.
    - Collapses whitespace into spaces.
    - Truncates to 1000 chars with an ellipsis.

    Non-string inputs are returned unchanged.

    Args:
        value (Any): Input, expected str.

    Returns:
        str or Any: Sanitized string, or original value if not a str.
    """
    if not isinstance(value, str):
        return value
    
    # First, normalize Unicode characters to ASCII where possible
    try:
        # Try to normalize unicode to ASCII equivalents
        normalized = unicodedata.normalize('NFKD', value)
        ascii_value = normalized.encode('ascii', 'ignore').decode('ascii')
    except:
        # If normalization fails, use original value
        ascii_value = value
    
    # Replace problematic characters and whitespace
    sanitized = (ascii_value.replace('\r\n', ' ')
                           .replace('\n', ' ')
                           .replace('\r', ' ')
                           .replace('\t', ' ')
                           .replace('"', "'")
                           .replace('\\', '/')
                           .replace('\x00', '')  # Remove null bytes
                           .strip())
    
    # Remove any remaining control characters
    sanitized = ''.join(char for char in sanitized if ord(char) >= 32 or char in ['\n', '\r', '\t'])
    
    # Collapse multiple spaces into single space
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    
    # Limit string length to prevent overly long values
    max_length = 1000
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    
    return sanitized

def sanitize_job_for_json(job_dict):
    """Recursively apply JSON-safe sanitization to all strings in a job dict.

    Walks through each value:
      - If str, passes through sanitize_string_for_json.
      - If list, sanitizes each string element.
      - Otherwise leaves it intact.

    Args:
        job_dict (dict): Raw job fields.

    Returns:
        dict: New dict with all text fields cleaned for JSON.
    """
    sanitized_job = {}
    for key, value in job_dict.items():
        if isinstance(value, str):
            sanitized_job[key] = sanitize_string_for_json(value)
        elif isinstance(value, list):
            # Handle list of strings
            sanitized_job[key] = [sanitize_string_for_json(item) if isinstance(item, str) else item for item in value]
        else:
            sanitized_job[key] = value
    return sanitized_job

@job_bp.route('/search_jobs', methods=['POST'])
def search_jobs():
    """Start a job search, sync or async depending on config & size.

    Expects JSON body with:
      - search_terms (dict)
      - desired_position (str)
      - target_location (str)
      - results_wanted (int, falls back to config default)
      - filename (str)
      - keywords (dict)

    If analysis is on or results_wanted > 10, spins off a background thread,
    seeds initial progress, and returns {'success': True, 'job_id': ...}. Otherwise
    runs synchronously and returns the full JSON payload.

    Returns:
        Response: Flask JSON response with either job_id for polling or final data.
    """
    config = get_config()
    logging.info("Job search request received")
    
    try:
        # Get data from form
        data = request.get_json()
        
        search_terms = data.get('search_terms', {})
        desired_position = data.get('desired_position', '')
        target_location = data.get('target_location', '')
        results_wanted = int(data.get('results_wanted', config.get_default_job_results()))
        filename = data.get('filename', 'resume')
        keywords = data.get('keywords', {})
        
        logging.info(f"Job search parameters - Position: {desired_position}, Location: {target_location}, Results: {results_wanted}")
        
        # Generate unique job ID for progress tracking
        job_id = str(uuid.uuid4())
        
        # Check if progress tracking should be enabled (when analysis is enabled or results > 10)
        enable_progress = config.get_job_analysis_enabled() or results_wanted > 10
        
        if enable_progress:
            # Start background job and return job ID for progress tracking
            update_job_progress(job_id, 'initializing', 0, 'Starting job search...')
            
            # Get the job results folder path from the current app context
            job_results_folder = current_app.config['JOB_RESULTS_FOLDER']
            
            # Start background thread
            thread = threading.Thread(
                target=perform_job_search_with_progress,
                args=(job_id, search_terms, desired_position, target_location, results_wanted, filename, keywords, config, job_results_folder)
            )
            thread.daemon = True
            thread.start()
            
            return jsonify({
                'success': True,
                'job_id': job_id,
                'message': 'Job search started. Use the job_id to track progress.'
            })
        else:
            # For simple searches, process synchronously
            job_results_folder = current_app.config['JOB_RESULTS_FOLDER']
            return perform_simple_job_search(search_terms, desired_position, target_location, results_wanted, filename, keywords, config, job_results_folder)
            
    except Exception as e:
        logging.error(f"Error starting job search: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

def perform_simple_job_search(search_terms, desired_position, target_location, results_wanted, filename, keywords, config, job_results_folder):
    """Run a quick, synchronous scrape and return results immediately.

    Builds search term and location, calls scrape_jobs, truncates to
    `results_wanted`, writes a CSV to `job_results_folder`, converts to
    JSON-safe dicts, and returns a Flask JSON response.

    Args:
        search_terms (dict): primary_search_terms, location overrides, etc.
        desired_position (str): Position title to prepend.
        target_location (str): Fallback geo location.
        results_wanted (int): Max jobs.
        filename (str): Base filename for output.
        keywords (dict): (Ignored here; analysis off).
        config: App config object.
        job_results_folder (str): Directory for CSV output.

    Returns:
        Response: jsonify({... jobs, count, search_params, output_file, analysis flags ...})
    """
    try:
        # Prepare search parameters
        primary_terms = search_terms.get("primary_search_terms", ["software engineer"])
        search_term = primary_terms[0] if primary_terms else "software engineer"
        
        if desired_position and desired_position.lower() not in search_term.lower():
            search_term = f"{desired_position} {search_term}".strip()
        
        location = search_terms.get("location", target_location or config.get('job_search.default_location', 'Remote'))
        google_search = search_terms.get("google_search_string", f"{search_term} jobs near {location}")
        
        logging.info(f"Executing simple job search - Term: '{search_term}', Location: '{location}'")
        
        # Perform job search
        jobs = scrape_jobs(
            site_name=config.get_job_search_sites(),
            search_term=search_term,
            google_search_term=google_search,
            location=location,
            results_wanted=results_wanted,
            hours_old=config.get_job_hours_old(),
            country_indeed=config.get('job_search.default_country', 'USA')
        )
        
        # Truncate if needed
        if len(jobs) > results_wanted:
            jobs = jobs.head(results_wanted)
        
        # Generate output filename and save results
        output_filename = generate_output_filename(filename, desired_position)
        output_path = os.path.join(job_results_folder, output_filename)
        jobs.to_csv(output_path, quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False)
        
        # Convert to response format
        jobs_list = convert_jobs_to_response_format(jobs, config)
        
        return jsonify({
            'success': True,
            'jobs': jobs_list,
            'count': len(jobs_list),
            'search_params': {
                'search_term': search_term,
                'location': location,
                'results_wanted': results_wanted
            },
            'output_file': output_filename,
            'analysis_enabled': False,
            'jobs_analyzed': False
        })
        
    except Exception as e:
        logging.error(f"Error in simple job search: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

def perform_job_search_with_progress(job_id, search_terms, desired_position, target_location, results_wanted, filename, keywords, config, job_results_folder):
    """Background job that scrapes, optionally analyzes, and updates progress.

    Workflow:
      1. initializing → scraping (5%→50%)
      2. scrape via scrape_jobs()
      3. optionally analyze in batches (50%→95%) via analyze_jobs_with_progress
      4. write final CSV, convert to list
      5. store complete progress with results under job_progress:<job_id>
      6. schedule in-memory cleanup in ~5m

    Args:
        job_id (str): Job UUID.
        search_terms (dict): As above.
        desired_position (str): As above.
        target_location (str): As above.
        results_wanted (int): As above.
        filename (str): As above.
        keywords (dict): Keywords for deeper analysis.
        config: App config.
        job_results_folder (str): Where to drop CSV.

    Returns:
        list[dict]: Sanitized job dicts, same format as the API payload.
    """
    try:
        update_job_progress(job_id, 'scraping', 5, 'Preparing job search...')
        
        # Prepare search parameters
        primary_terms = search_terms.get("primary_search_terms", ["software engineer"])
        search_term = primary_terms[0] if primary_terms else "software engineer"
        
        if desired_position and desired_position.lower() not in search_term.lower():
            search_term = f"{desired_position} {search_term}".strip()
        
        location = search_terms.get("location", target_location or config.get('job_search.default_location', 'Remote'))
        google_search = search_terms.get("google_search_string", f"{search_term} jobs near {location}")
        
        logging.info(f"Executing job search with progress - Term: '{search_term}', Location: '{location}'")
        
        update_job_progress(job_id, 'scraping', 15, f'Searching for "{search_term}" jobs...')
        
        # Perform job search
        jobs = scrape_jobs(
            site_name=config.get_job_search_sites(),
            search_term=search_term,
            google_search_term=google_search,
            location=location,
            results_wanted=results_wanted,
            hours_old=config.get_job_hours_old(),
            country_indeed=config.get('job_search.default_country', 'USA')
        )
        
        initial_job_count = len(jobs)
        update_job_progress(job_id, 'scraping', 40, f'Found {initial_job_count} jobs, processing...')
        
        # Truncate if needed
        if len(jobs) > results_wanted:
            jobs = jobs.head(results_wanted)
        
        update_job_progress(job_id, 'scraping', 50, f'Using {len(jobs)} jobs for analysis...')
        
        # Job Analysis Phase
        jobs_analyzed = False
        analysis_summary = None
        
        if config.get_job_analysis_enabled() and len(jobs) > 0:
            update_job_progress(job_id, 'analyzing', 50, 'Starting job analysis...')
            
            try:
                # Initialize processor for job analysis
                processor = ResumeProcessor()
                
                # Convert jobs DataFrame to list of dictionaries
                jobs_list = jobs.to_dict('records')
                
                # Calculate analysis batches for progress tracking
                batch_size = config.get_job_analysis_batch_size()
                total_batches = (len(jobs_list) + batch_size - 1) // batch_size
                
                update_job_progress(job_id, 'analyzing', 55, f'Analyzing {len(jobs_list)} jobs in {total_batches} batches...',
                                  analysis_progress={
                                      'completed_batches': 0,
                                      'total_batches': total_batches,
                                      'current_batch': {'jobs_in_batch': 0}
                                  })
                
                # Analyze jobs with progress updates
                analyzed_jobs_list = analyze_jobs_with_progress(
                    job_id, processor, jobs_list, keywords, batch_size, total_batches
                )
                
                # Convert back to DataFrame
                jobs = pd.DataFrame(analyzed_jobs_list)
                jobs_analyzed = True
                
                analyzed_count = sum(1 for job in analyzed_jobs_list if job.get('analyzed', False))
                salary_extracted_count = sum(1 for job in analyzed_jobs_list 
                                           if job.get('salary_min_extracted') or job.get('salary_max_extracted'))
                
                analysis_summary = {
                    'analyzed_count': analyzed_count,
                    'total_count': len(analyzed_jobs_list),
                    'salary_extracted_count': salary_extracted_count
                }
                
                update_job_progress(job_id, 'analyzing', 95, f'Analysis completed - {analyzed_count} jobs analyzed')
                
            except Exception as e:
                logging.error(f"Error during job analysis: {str(e)}", exc_info=True)
                update_job_progress(job_id, 'analyzing', 80, 'Analysis failed, continuing with basic results...')
        
        # Save results - use the passed job_results_folder instead of current_app.config
        output_filename = generate_output_filename(filename, desired_position)
        output_path = os.path.join(job_results_folder, output_filename)
        jobs.to_csv(output_path, quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False)
        
        # Convert to response format
        jobs_list = convert_jobs_to_response_format(jobs, config)
        
        # Final results
        results = {
            'success': True,
            'jobs': jobs_list,
            'count': len(jobs_list),
            'search_params': {
                'search_term': search_term,
                'location': location,
                'results_wanted': results_wanted,
                'initial_scraped_count': initial_job_count,
                'final_returned_count': len(jobs_list)
            },
            'output_file': output_filename,
            'analysis_enabled': config.get_job_analysis_enabled(),
            'jobs_analyzed': jobs_analyzed,
            'analysis_summary': analysis_summary
        }
        
        # Mark as complete with results
        final_progress = {
            'phase': 'complete',
            'percent': 100,
            'details': 'Job search completed!',
            'analysis_progress': None,
            'results': results
        }
        
        # Store final progress with results using the proper storage method
        if REDIS_AVAILABLE:
            redis_client = get_redis_client()
            if redis_client:
                try:
                    redis_client.setex(
                        f"job_progress:{job_id}", 
                        3600,  # Keep results for 1 hour
                        json.dumps(final_progress)
                    )
                except Exception as e:
                    logging.warning(f"Failed to store final results in Redis: {e}")
                    # Fallback to in-memory
                    with progress_lock:
                        job_progress_storage[job_id] = final_progress
            else:
                # Fallback to in-memory
                with progress_lock:
                    job_progress_storage[job_id] = final_progress
        else:
            # In-memory storage
            with progress_lock:
                job_progress_storage[job_id] = final_progress
        
        logging.info(f"Background job search completed for job_id: {job_id}")
        
        # Clean up after some time (in a real app, use a proper job queue)
        def cleanup_later():
            import time
            time.sleep(300)  # Wait 5 minutes
            cleanup_job_progress(job_id)
        
        cleanup_thread = threading.Thread(target=cleanup_later)
        cleanup_thread.daemon = True
        cleanup_thread.start()
        
        return jobs_list

    except Exception as e:
        logging.error(f"Error in background job search: {str(e)}", exc_info=True)
        update_job_progress(job_id, 'error', 0, f'Error: {str(e)}')

def analyze_jobs_with_progress(job_id, processor, jobs_list, keywords, batch_size, total_batches):
    """Analyze jobs in slices, updating percent status after each batch.

    Splits jobs_list into `batch_size`, calls
    `processor._analyze_job_batch` (or default on error), sleeps 0.1s
    per batch to smooth UI. Progress goes from 55% up to 95%.

    Args:
        job_id (str): Same UUID for progress key.
        processor (ResumeProcessor): Must support _analyze_job_batch() etc.
        jobs_list (list[dict]): Raw scraped jobs.
        keywords (dict): Analysis keywords.
        batch_size (int): Jobs per batch.
        total_batches (int): Number of batches.

    Returns:
        list[dict]: Inputs augmented with analysis results.
    """
    analyzed_jobs = []
    
    for batch_idx in range(total_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, len(jobs_list))
        batch = jobs_list[start_idx:end_idx]
        
        # Update progress for current batch
        progress_percent = 55 + (batch_idx / total_batches) * 40  # 55% to 95%
        
        update_job_progress(job_id, 'analyzing', progress_percent, 
                          f'Analyzing batch {batch_idx + 1}/{total_batches}...',
                          analysis_progress={
                              'completed_batches': batch_idx,
                              'total_batches': total_batches,
                              'current_batch': {'jobs_in_batch': len(batch)}
                          })
        
        # Analyze the batch (simplified - using the existing method)
        try:
            analyzed_batch = processor._analyze_job_batch(batch, keywords)
            analyzed_jobs.extend(analyzed_batch)
        except Exception as e:
            logging.error(f"Error analyzing batch {batch_idx + 1}: {str(e)}")
            # Add default analysis for failed batch
            analyzed_jobs.extend(processor._create_default_analysis(batch))
        
        # Small delay to make progress visible and be nice to APIs
        import time
        time.sleep(0.1)
    
    # Final batch update
    update_job_progress(job_id, 'analyzing', 95, 'Analysis completed!',
                      analysis_progress={
                          'completed_batches': total_batches,
                          'total_batches': total_batches,
                          'current_batch': None
                      })
    
    return analyzed_jobs

def generate_output_filename(filename, desired_position):
    """Build a timestamped CSV filename for job results.

    Strips any prefix up to first '_', uses the remainder as resume_name,
    appends lowercased desired_position (underscored), plus YYYYMMDD_HHMMSS.

    Args:
        filename (str): Original name, e.g. 'resume_MyName.pdf'.
        desired_position (str): Job title to include.

    Returns:
        str: 'jobs_{resume_name}_{position}_{timestamp}.csv'
    """
    resume_name = os.path.splitext(filename.split('_', 1)[1] if '_' in filename else filename)[0]
    position_suffix = f"_{desired_position.replace(' ', '_').lower()}" if desired_position else ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"jobs_{resume_name}{position_suffix}_{timestamp}.csv"

def convert_jobs_to_response_format(jobs, config):
    """Turn a DataFrame of jobs into JSON-safe dicts for the API.

    - Truncates descriptions to config['job_search.description_max_length'].
    - Fills missing fields with 'N/A' or empty.
    - Injects analysis keys if job['analyzed'] is True.
    - Runs each dict through sanitize_job_for_json.

    Args:
        jobs (pd.DataFrame): Scraped/processed jobs.
        config: App config.

    Returns:
        list[dict]: Clean payload ready for jsonify().
    """
    jobs_list = []
    description_max_length = config.get('job_search.description_max_length', 500)
    
    for i, job in jobs.iterrows():
        # Safely handle description field that might be float/NaN
        description = job.get('description', '')
        if not isinstance(description, str):
            if pd.isna(description):
                description = ''
            else:
                description = str(description)
        
        job_dict = {
            'title': job.get('title', 'N/A') if pd.notna(job.get('title', '')) else 'N/A',
            'company': job.get('company', 'N/A') if pd.notna(job.get('company', '')) else 'N/A',
            'location': job.get('location', 'N/A') if pd.notna(job.get('location', '')) else 'N/A',
            'site': job.get('site', 'N/A') if pd.notna(job.get('site', '')) else 'N/A',
            'job_url': job.get('job_url', '') if pd.notna(job.get('job_url', '')) else '',
            'description': description[:description_max_length] + '...' if description else '',
            'salary_min': job.get('salary_min', '') if pd.notna(job.get('salary_min', '')) else '',
            'salary_max': job.get('salary_max', '') if pd.notna(job.get('salary_max', '')) else '',
            'date_posted': str(job.get('date_posted', '')) if pd.notna(job.get('date_posted', '')) else ''
        }
        
        # Add analysis data if available
        if job.get('analyzed', False):
            job_dict.update({
                'analyzed': job.get('analyzed', False),
                'similarity_score': job.get('similarity_score', 0.0) if pd.notna(job.get('similarity_score', 0.0)) else 0.0,
                'similarity_explanation': job.get('similarity_explanation', '') if pd.notna(job.get('similarity_explanation', '')) else '',
                'salary_min_extracted': job.get('salary_min_extracted') if pd.notna(job.get('salary_min_extracted')) else None,
                'salary_max_extracted': job.get('salary_max_extracted') if pd.notna(job.get('salary_max_extracted')) else None,
                'salary_confidence': job.get('salary_confidence', 0.0) if pd.notna(job.get('salary_confidence', 0.0)) else 0.0,
                'key_matches': job.get('key_matches', []) if isinstance(job.get('key_matches'), list) else [],
                'missing_requirements': job.get('missing_requirements', []) if isinstance(job.get('missing_requirements'), list) else []
            })
        
        # Sanitize the job dictionary for JSON safety
        job_dict = sanitize_job_for_json(job_dict)
        jobs_list.append(job_dict)
    
    return jobs_list

@job_bp.route('/job_progress/<job_id>')
def get_progress(job_id):
    """Endpoint to fetch real-time progress for a given job_id.

    Checks Redis first, then in-memory store. If nothing’s found,
    returns 404 with {'error': 'Job not found or completed'}.

    Args:
        job_id (str): UUID from `/search_jobs`.

    Returns:
        Response: JSON with keys phase, percent, details, analysis_progress, timestamp.
    """
    logging.info(f"Progress requested for job_id: {job_id}")
    
    progress = get_job_progress(job_id)
    
    if not progress:
        logging.warning(f"No progress found for job_id: {job_id}")
        return jsonify({'error': 'Job not found or completed'}), 404
    
    logging.info(f"Progress found for job_id: {job_id}, phase: {progress.get('phase', 'unknown')}")
    return jsonify(progress) 