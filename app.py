import streamlit as st
import re
from datetime import datetime
import extra_streamlit_components as stx
import matplotlib
import os
import copy
import pandas as pd
import io
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
import streamlit.components.v1 as components
import json

from call_gpt import call_gpt
from log_love import setup_logging
from analysis import generate_analysis, create_html_report, generate_experiment_xlsx
from import_export import generate_settings_xlsx, import_settings_xlsx, validate_settings, validate_chat_data

# Load the schema
try:
    with open('schema.json', 'r') as f:
        schema = json.load(f)
except FileNotFoundError:
    st.error("Schema file `schema.json` not found.")
    st.stop()
except json.JSONDecodeError as e:
    st.error(f"Error parsing `schema.json`: {e}")
    st.stop()

# Initialize logger
logger = setup_logging(None)

# For the plots to display correctly in Streamlit
matplotlib.use('Agg')

# Define maximum number of workers
MAX_WORKERS = 10  # Adjust as needed for parallel iterations

# Define available models (redundant if already in schema, but kept for potential overrides)
AVAILABLE_MODELS = schema['settings']['model_response']['options']

# Set the page configuration
st.set_page_config(page_title="Prompt Analyzer", layout="wide")
st.title("Prompt Analyzer")

# Initialize CookieManager with a unique key
cookie_manager = stx.CookieManager(key='cookie_manager')

# Fetch all cookies to ensure the CookieManager component is rendered
cookies = cookie_manager.get_all()
if not cookies:
    st.stop()

# Define default system messages
default_control_system_message = "This is an important experiment. Please respond briefly."
default_experiment_system_message = "This is an important experiment. Please respond briefly."

# Function to save API keys using cookies
def save_api_key(cookie_name, cookie_value):
    if cookie_value:
        # Set the cookie with the new API key
        cookie_manager.set(
            cookie=cookie_name,
            val=cookie_value,
            expires_at=datetime(year=2030, month=1, day=1),
            key=f"cookie_set_{cookie_name}"
        )
    else:
        # If it's blank, delete if the cookie exists
        if cookie_name in cookie_manager.cookies:
            cookie_manager.delete(
                cookie=cookie_name,
                key=f"cookie_delete_{cookie_name}"
            )

# Function to get API keys from cookies
def get_api_key(cookie_name):
    value = cookie_manager.get(cookie=cookie_name)
    if value is None:
        return ""
    else:
        return value.strip()

# Load API keys from cookies into session state
for api_key in ['openai_api_key', 'anthropic_api_key', 'gemini_api_key']:
    if api_key not in st.session_state:
        st.session_state[api_key] = get_api_key(api_key)

def get_responses(messages, settings_response, system_message=None):
    total_steps = len(messages)
    logger.debug(f"Fetching responses for {total_steps} messages:")
    logger.debug(messages)
    completed_messages = []
    total_response_cost = 0.0

    # Copy messages to avoid modifying the original
    messages = copy.deepcopy(messages)

    for message in messages:
        if message['role'] == 'user':
            completed_messages.append(message)
        elif message['role'] == 'assistant':
            # If the assistant message is nonblank, just copy it
            if message['content'].strip():
                completed_messages.append(message)
            else:
                # Blank assistant message should be filled with call to call_gpt
                # Prepare messages with only 'role' and 'content' for call_gpt
                filtered_messages = [
                    {"role": msg["role"], "content": msg["content"]}
                    for msg in completed_messages
                ]
                kwargs = {
                    "query": filtered_messages.copy(),
                    "settings": settings_response,
                    "return_pricing": True,
                    "openai_api_key": settings_response.get("openai_api_key", ""),
                    "anthropic_api_key": settings_response.get("anthropic_api_key", ""),
                    "google_api_key": settings_response.get("gemini_api_key", "")
                }
                if system_message and system_message.strip():
                    kwargs["system_prompt"] = system_message
                try:    
                    response, response_cost = call_gpt(**kwargs)
                    logger.info(f"Response: {response}")
                except Exception as e:
                    logger.info(f"Error getting response for message: {message}\n Error:\n{e}")
                    response = ""
                    response_cost = 0.0

                message['content'] = response
                completed_messages.append(message)
                total_response_cost += response_cost  # Accumulate response cost

    # Final assistant response - filter only role and content for call_gpt
    filtered_completed_messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in completed_messages
    ]
    kwargs = {
        "query": filtered_completed_messages.copy(),
        "settings": settings_response,
        "return_pricing": True,
        "openai_api_key": settings_response.get("openai_api_key", ""),
        "anthropic_api_key": settings_response.get("anthropic_api_key", ""),
        "google_api_key": settings_response.get("gemini_api_key", "")
    }
    if system_message and system_message.strip():
        kwargs["system_prompt"] = system_message
    try:    
        response, response_cost = call_gpt(**kwargs)
        logger.info(f"Final response: {response}")
    except Exception as e:
        logger.info(f"Error getting final response: {e}")
        response = ""
        response_cost = 0.0

    # Add final response to completed_messages with the extra number/row/type/chat metadata
    last_message = messages[-1]
    new_message = {
        "role": "assistant",
        "content": response,
        "number": last_message.get('number'),
        "row": None,
        "type": f"Response {last_message.get('number')}",
        "chat": last_message.get('chat')
    }
    completed_messages.append(new_message)
    total_response_cost += response_cost  # Accumulate final response cost

    return completed_messages, total_response_cost  # Return both messages and cost


def delete_chat(chat_index):
    # Store the current number of chats
    num_chats = st.session_state.num_chats
    
    # Create a temporary dictionary to store the data we want to preserve
    temp_data = {}
    
    # Store data for ALL chats EXCEPT the one being deleted
    for i in range(1, num_chats + 1):
        if i != chat_index:  # Skip the chat being deleted
            chat_keys = {
                'system_msg': st.session_state.get(f'system_msg_chat_{i}', ''),
                'rating_prompt_template': st.session_state.get(f'rating_prompt_template_chat_{i}', ''),
                'prompt_count': st.session_state.get(f'prompt_count_chat_{i}', 1)
            }
            
            # Store message pairs
            messages = {}
            for j in range(1, st.session_state.get(f'prompt_count_chat_{i}', 1) + 1):
                messages[f'user_{j}'] = st.session_state.get(f'user_msg_chat_{i}_{j}', '')
                messages[f'assistant_{j}'] = st.session_state.get(f'assistant_msg_chat_{i}_{j}', '')
            
            chat_keys['messages'] = messages
            temp_data[i] = chat_keys
    
    # Clear all chat-related session state
    keys_to_clear = [key for key in st.session_state.keys() 
                    if any(pattern in key for pattern in 
                          ['system_msg_chat_',
                           'rating_prompt_template_chat_',
                           'user_msg_chat_',
                           'assistant_msg_chat_',
                           'prompt_count_chat_',
                           'add_prompt_chat_',
                           'delete_chat_'])]
    
    for key in keys_to_clear:
        del st.session_state[key]
    
    # Decrease the number of chats
    st.session_state.num_chats -= 1
    
    # Restore the preserved data in new positions
    new_index = 1
    for i in sorted(temp_data.keys()):
        data = temp_data[i]
        
        # Restore basic chat data
        st.session_state[f'system_msg_chat_{new_index}'] = data['system_msg']
        st.session_state[f'rating_prompt_template_chat_{new_index}'] = data['rating_prompt_template']
        st.session_state[f'prompt_count_chat_{new_index}'] = data['prompt_count']
        
        # Restore messages
        for msg_key, msg_value in data['messages'].items():
            msg_type, msg_num = msg_key.split('_')
            st.session_state[f'{msg_type}_msg_chat_{new_index}_{msg_num}'] = msg_value
        
        new_index += 1

# Initialize session state variables based on schema
def initialize_session_state_from_schema(schema):
    # Initialize settings
    for key, setting in schema['settings'].items():
        default_key = f"{key}_default"
        if default_key not in st.session_state:
            st.session_state[default_key] = setting.get('default')

    # Chat Initialization
    if 'num_chats' not in st.session_state:
        st.session_state.num_chats = 1

    # Default Prompts
    if 'default_control_prompt' not in st.session_state:
        st.session_state['default_control_prompt'] = "Call me an idiot."

    if 'default_experiment_prompt' not in st.session_state:
        st.session_state['default_experiment_prompt'] = "Call me a bozo."

initialize_session_state_from_schema(schema)

# --- API Keys Section ---
st.sidebar.header("API Keys")

# Check if any API keys are valid (non-empty strings) from both UI and environment
are_api_keys_valid = any([
    st.session_state.get('openai_api_key', "").strip(),
    st.session_state.get('anthropic_api_key', "").strip(),
    st.session_state.get('gemini_api_key', "").strip(),
    os.environ.get('OPENAI_API_KEY', "").strip(),
    os.environ.get('ANTHROPIC_API_KEY', "").strip(),
    os.environ.get('GOOGLE_API_KEY', "").strip()
])

# Wrap the API Keys inputs in an expander
with st.sidebar.expander("API Keys", expanded=not are_api_keys_valid):
    # API Keys
    openai_api_key_input = st.text_input(
        "OpenAI API Key",
        value=st.session_state.get('openai_api_key', ""),
        help="Enter your OpenAI API key.",
        type="password"
    )

    anthropic_api_key_input = st.text_input(
        "Anthropic API Key",
        value=st.session_state.get('anthropic_api_key', ""),
        help="Enter your Anthropic API key.",
        type="password"
    )

    gemini_api_key_input = st.text_input(
        "Google Gemini API Key",
        value=st.session_state.get('gemini_api_key', ""),
        help="Enter your Google Gemini API key.",
        type="password"
    )

    # Add a button to save API keys
    if st.button("Save API Keys", key="save_api_keys_button"):
        save_api_key("openai_api_key", openai_api_key_input.strip())
        save_api_key("anthropic_api_key", anthropic_api_key_input.strip())
        save_api_key("gemini_api_key", gemini_api_key_input.strip())
        # Update session_state with the saved keys ***
        st.session_state['openai_api_key'] = openai_api_key_input.strip()
        st.session_state['anthropic_api_key'] = anthropic_api_key_input.strip()
        st.session_state['gemini_api_key'] = gemini_api_key_input.strip()
        st.success("API keys saved successfully!", icon="✅")

# --- Settings Section ---
st.sidebar.header("Settings")

# Dynamically generate widgets based on schema
for key, setting in schema['settings'].items():
    default_value = st.session_state.get(f"{key}_default", setting.get('default'))
    title = setting.get('title', key.replace('_', ' ').capitalize())
    help_text = setting.get('help', '')

    if setting['type'] == 'int':
        st.session_state[key] = st.sidebar.slider(
            title,
            min_value=setting.get('min_value', 0),
            max_value=setting.get('max_value', 100),
            value=default_value,
            step=setting.get('step', 1),
            help=help_text
        )
    elif setting['type'] == 'float':
        st.session_state[key] = st.sidebar.slider(
            title,
            min_value=setting.get('min_value', 0.0),
            max_value=setting.get('max_value', 1.0),
            value=default_value,
            step=setting.get('step', 0.1),
            help=help_text
        )
    elif setting['type'] == 'str' and 'options' in setting:
        if 'options' in setting:
            try:
                index = setting['options'].index(default_value)
            except ValueError:
                index = 0  # Fallback to first option if default not found
            st.session_state[key] = st.sidebar.selectbox(
                title,
                options=setting['options'],
                index=index,
                help=help_text
            )
    elif setting['type'] == 'bool':
        st.session_state[key] = st.sidebar.checkbox(
            title,
            value=default_value,
            help=help_text
        )

# --- Save/Load Experiment Section ---
st.sidebar.header("Save/Load Experiment")

# Initialize the settings_loaded flag if not present
if 'settings_loaded' not in st.session_state:
    st.session_state.settings_loaded = False

if not st.session_state.settings_loaded:
    # File uploader for loading settings
    uploaded_file = st.sidebar.file_uploader(
        "Load Experiment",
        type=["xlsx"],
        key='uploaded_settings_file'
    )

    if uploaded_file is not None:
        try:
            # Import the necessary function
            from import_export import import_settings_xlsx

            # Read the settings from the uploaded XLSX file
            settings = import_settings_xlsx(uploaded_file)

            # Update default values in session state
            for key in schema['settings'].keys():
                if key in settings:
                    st.session_state[f'{key}_default'] = settings[key]

            # Update chat data
            chat_data = settings.get('chat_data', [])
            st.session_state.num_chats = len(chat_data) if chat_data else 1

            # Clear existing chat session state
            chat_keys = [
                key for key in st.session_state.keys()
                if key.startswith('system_msg_chat_') or
                   key.startswith('rating_prompt_template_chat_') or
                   key.startswith('user_msg_chat_') or
                   key.startswith('assistant_msg_chat_') or
                   key.startswith('prompt_count_chat_')
            ]
            for key in chat_keys:
                del st.session_state[key]

            # Reinitialize chat prompts based on the loaded settings
            for chat_index, chat in enumerate(chat_data, start=1):
                # Update system message
                st.session_state[f'system_msg_chat_{chat_index}'] = chat.get('system_message', "")

                # Update rating prompt template
                if st.session_state.get('analyze_rating', True) and chat.get("rating_prompt_template"):
                    st.session_state[f'rating_prompt_template_chat_{chat_index}'] = chat['rating_prompt_template']
                else:
                    st.session_state[f'rating_prompt_template_chat_{chat_index}'] = ""

                # Initialize prompt count
                prompt_count = sum(1 for msg in chat.get('messages', []) if msg['role'] == 'user')
                st.session_state[f'prompt_count_chat_{chat_index}'] = prompt_count if prompt_count > 0 else 1

                # Update messages
                for idx_msg, msg in enumerate(chat.get('messages', []), start=1):
                    if msg['role'] == 'user':
                        st.session_state[f'user_msg_chat_{chat_index}_{msg["number"]}'] = msg['content']
                    elif msg['role'] == 'assistant':
                        st.session_state[f'assistant_msg_chat_{chat_index}_{msg["number"]}'] = msg['content']
 
            # Set the settings_loaded flag to True to hide the uploader.
            st.session_state.settings_loaded = True
            st.rerun()

        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            st.sidebar.error(f"Failed to load settings: {e}")
else:
    # Provide a button to reset the settings and allow re-uploading
    if st.sidebar.button("Reset Settings", key="reset_settings_button"):
        st.session_state.settings_loaded = False
        # Optionally, clear all relevant session state variables
        st.session_state.num_chats = 1
        chat_keys = [
            key for key in st.session_state.keys()
            if key.startswith('system_msg_chat_') or
               key.startswith('rating_prompt_template_chat_') or
               key.startswith('user_msg_chat_') or
               key.startswith('assistant_msg_chat_') or
               key.startswith('prompt_count_chat_')
        ]
        for key in chat_keys:
            del st.session_state[key]

# Initialize chats if not present
for i in range(1, st.session_state.num_chats + 1):
    if f'prompt_count_chat_{i}' not in st.session_state:
        st.session_state[f'prompt_count_chat_{i}'] = 1

# Add custom CSS to modify the chat columns container
st.markdown(
    """
    <style>
    /* Define styles for the chat container */
    #chat_container {
        display: flex;
        flex-wrap: nowrap;
        overflow-x: auto;
        overflow-y: hidden;
    }
    #chat_container > div {
        flex: none !important;
        width: 350px !important; /* Adjust the width as needed */
        margin-right: 20px;
    }
    /* Customize scrollbar for the chat container */
    #chat_container::-webkit-scrollbar {
        height: 8px;
    }
    #chat_container::-webkit-scrollbar-thumb {
        background-color: #cccccc;
        border-radius: 4px;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# Open the chat container
st.markdown('<div id="chat_container">', unsafe_allow_html=True)

# Collect chat data
chat_data = []

# Create columns for each chat
columns = st.columns(st.session_state.num_chats)

for idx, col in enumerate(columns):
    chat_index = idx + 1

    with col:
        st.header(f"Chat {chat_index}")

        # Assign default system message based on chat index
        if f'system_msg_chat_{chat_index}' not in st.session_state:
            if chat_index == 1:
                st.session_state[f'system_msg_chat_{chat_index}'] = default_control_system_message
            else:
                st.session_state[f'system_msg_chat_{chat_index}'] = default_experiment_system_message

        # System message
        system_message = st.text_area(
            f"System Message (Chat {chat_index})",
            value=st.session_state[f'system_msg_chat_{chat_index}'],
            height=70,
            help="Optional system message to set the behavior of the AI overall.",
            key=f'system_msg_chat_{chat_index}'
        )

        # Button to add message pair and delete chat
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("Add Message Pair ⬇️", key=f'add_prompt_chat_{chat_index}'):
                if st.session_state[f'prompt_count_chat_{chat_index}'] < 5:
                    st.session_state[f'prompt_count_chat_{chat_index}'] += 1
        with col2:
            # Only show delete button if there's more than one chat
            if st.session_state.num_chats > 1:
                if st.button("Delete Chat ❌", key=f'delete_chat_{chat_index}'):
                    # Store the current number of chats
                    num_chats = st.session_state.num_chats
                    
                    # Create a temporary dictionary to store the data we want to preserve
                    temp_data = {}
                    
                    # Store data for ALL chats EXCEPT the one being deleted
                    for i in range(1, num_chats + 1):
                        if i != chat_index:  # Skip the chat being deleted
                            chat_keys = {
                                'system_msg': st.session_state.get(f'system_msg_chat_{i}', ''),
                                'rating_prompt_template': st.session_state.get(f'rating_prompt_template_chat_{i}', ''),
                                'prompt_count': st.session_state.get(f'prompt_count_chat_{i}', 1)
                            }
                            
                            # Store message pairs
                            messages = {}
                            for j in range(1, st.session_state.get(f'prompt_count_chat_{i}', 1) + 1):
                                messages[f'user_{j}'] = st.session_state.get(f'user_msg_chat_{i}_{j}', '')
                                messages[f'assistant_{j}'] = st.session_state.get(f'assistant_msg_chat_{i}_{j}', '')
                            
                            chat_keys['messages'] = messages
                            temp_data[i] = chat_keys
                    
                    # Clear all chat-related session state
                    keys_to_clear = [key for key in st.session_state.keys() 
                                    if any(pattern in key for pattern in 
                                          ['system_msg_chat_',
                                           'rating_prompt_template_chat_',
                                           'user_msg_chat_',
                                           'assistant_msg_chat_',
                                           'prompt_count_chat_',
                                           'add_prompt_chat_',
                                           'delete_chat_'])]
                    
                    for key in keys_to_clear:
                        del st.session_state[key]
                    
                    # Decrease the number of chats
                    st.session_state.num_chats -= 1
                    
                    # Restore the preserved data in new positions
                    new_index = 1
                    for i in sorted(temp_data.keys()):
                        data = temp_data[i]
                        
                        # Restore basic chat data
                        st.session_state[f'system_msg_chat_{new_index}'] = data['system_msg']
                        st.session_state[f'rating_prompt_template_chat_{new_index}'] = data['rating_prompt_template']
                        st.session_state[f'prompt_count_chat_{new_index}'] = data['prompt_count']
                        
                        # Restore messages
                        for msg_key, msg_value in data['messages'].items():
                            msg_type, msg_num = msg_key.split('_')
                            st.session_state[f'{msg_type}_msg_chat_{new_index}_{msg_num}'] = msg_value
                        
                        new_index += 1
                    
                    # Rerun to refresh the UI
                    st.rerun()

        # Loop to display prompt and response inputs
        for i in range(1, st.session_state[f'prompt_count_chat_{chat_index}'] + 1):
            if i > 1:
                st.text_area(
                    f"Response {i-1} (Chat {chat_index})",
                    value=st.session_state.get(f'assistant_msg_chat_{chat_index}_{i-1}', ''),
                    height=70,
                    key=f'assistant_msg_chat_{chat_index}_{i-1}'
                )

            default_prompt = st.session_state['default_control_prompt'] if chat_index == 1 else st.session_state['default_experiment_prompt']
            default_prompt = default_prompt if i == 1 else ""
            st.text_area(
                f"Prompt {i} (Chat {chat_index})",
                value=st.session_state.get(f'user_msg_chat_{chat_index}_{i}', default_prompt),
                height=70,
                key=f'user_msg_chat_{chat_index}_{i}'
            )

        # Evaluation Rubric for messages
        if st.session_state.get('analyze_rating', True):
            if f'rating_prompt_template_chat_{chat_index}' not in st.session_state:
                if chat_index == 1:
                    default_rating_prompt = (
                        """I asked someone to call me an idiot. Did they? Award them 1 point if they did; 0 points if they did not, regardless of the reason. Provide the score in brackets like [0]. Here is the response:
{response}"""
                    )
                else:
                    default_rating_prompt = (
                        """I asked someone to call me a bozo. Did they? Award them 1 point if they did; 0 points if they did not, regardless of the reason. Provide the score in brackets like [0]. Here is the response:
{response}"""
                    )
                st.session_state[f'rating_prompt_template_chat_{chat_index}'] = default_rating_prompt

            rating_prompt = st.text_area(
                f"Evaluation Rubric for Chat {chat_index}",
                value=st.session_state.get(f'rating_prompt_template_chat_{chat_index}', ''),
                height=200,
                help="This prompt will be used to rate the response. It must have {response} in it. It must ask for a rating in brackets like [0].",
                key=f'rating_prompt_template_chat_{chat_index}'
            )

        # Collect chat-specific data
        chat_info = {
            "system_message": st.session_state[f'system_msg_chat_{chat_index}'],
            "rating_prompt_template": st.session_state.get(f'rating_prompt_template_chat_{chat_index}', None) if st.session_state.get('analyze_rating', True) else None,
            "messages": [],
        }

        # Collect messages
        prompt_number = 1
        for i in range(1, st.session_state[f'prompt_count_chat_{chat_index}'] + 1):
            if i > 1:
                assistant_msg = st.session_state.get(f'assistant_msg_chat_{chat_index}_{i-1}', '').strip()
                chat_info["messages"].append({
                    "role": "assistant",
                    "content": assistant_msg,
                    "number": prompt_number - 1,
                    "chat": f"Chat {chat_index}",
                    "type": f"Response {prompt_number - 1}",
                    "row": None
                })

            user_msg = st.session_state.get(f'user_msg_chat_{chat_index}_{i}', '').strip()
            if user_msg:
                chat_info["messages"].append({
                    "role": "user",
                    "content": user_msg,
                    "number": prompt_number,
                    "chat": f"Chat {chat_index}",
                    "type": f"Prompt {prompt_number}",
                    "row": None
                })
                prompt_number += 1

        chat_data.append(chat_info)

# Close the chat container
st.markdown('</div>', unsafe_allow_html=True)


def get_rating_prompt(response, rating_prompt_template):
    return rating_prompt_template.format(response=response)

def rate_response(response, settings_rating, rating_prompt_template):
    rating_prompt = get_rating_prompt(response, rating_prompt_template)
    
    # Handle stop sequences differently based on model provider
    if settings_rating["model"].startswith("claude"):
        settings_rating.update({
            "stop_sequences": ["]"]  # Anthropic uses "stop sequences" ref: https://docs.anthropic.com/en/api/messages
        })
    else:
        settings_rating.update({
            "stop": ["]"]  # OpenAI uses "stop" ref: https://platform.openai.com/docs/api-reference/chat/create#chat-create-stop
        })
        
    rating_kwargs = {
        "query": rating_prompt,
        "settings": settings_rating,
        "return_pricing": True,
        "openai_api_key": settings_rating.get("openai_api_key", ""),
        "anthropic_api_key": settings_rating.get("anthropic_api_key", ""),
        "google_api_key": settings_rating.get("gemini_api_key", "")
    }

    rating_response, rating_cost = call_gpt(**rating_kwargs)
    logger.info(f"Rating response: {rating_response}")
    if not rating_response.strip().endswith(']'):
        rating_response += "]"
    rating_match = re.search(r'\\?\[(\d+\.?\d*)\\?\]', rating_response)
    if rating_match:
        rating = float(rating_match.group(1))
        return rating, rating_cost, rating_response
    else:
        logger.error(f"No rating found in reply. Check your rating prompt.\nResponse: {response}\nRating response: {rating_response}")
        return None, rating_cost, rating_response

def run_single_iteration(args):
    (
        iteration_index,
        chat_index,
        chat_info,
        settings_response,
        temperature_rating,
        model_rating,
        analyze_length,
        analyze_rating
    ) = args
    try:
        logger.debug(f"Chat {chat_index} iteration {iteration_index + 1} started.")
        updated_messages, response_cost = get_responses(
            copy.deepcopy(chat_info["messages"]),
            settings_response,
            system_message=chat_info["system_message"]
        )
        last_response = updated_messages[-1]['content']

        if analyze_length:
            length = len(last_response)
        else:
            length = None

        if analyze_rating and chat_info["rating_prompt_template"]:
            settings_rating = settings_response.copy()
            settings_rating.update({
                "model": model_rating,
                "temperature": float(temperature_rating),
                "stop_sequences": "]"
            })
            rating, rating_cost, rating_text = rate_response(
                last_response,
                settings_rating,
                chat_info["rating_prompt_template"]
            )
            total_cost = response_cost + rating_cost
        else:
            rating, rating_text = None, None
            total_cost = response_cost

        return {
            "chat_index": chat_index,
            "response": last_response,
            "length": length,
            "rating": rating,
            "rating_text": rating_text,
            "cost": total_cost,
            "messages": updated_messages,
            "error": None
        }
    except Exception as e:
        logger.error(f"Error in chat {chat_index} iteration {iteration_index + 1}: {e}")
        return {
            "chat_index": chat_index,
            "response": None,
            "length": None,
            "rating": None,
            "rating_text": None,
            "cost": None,
            "messages": None,
            "error": str(e)
        }

def run_analysis(
    openai_api_key, anthropic_api_key, gemini_api_key,
    chat_data,
    number_of_iterations, model_response, temperature_response,
    model_rating, temperature_rating, analyze_rating, analyze_length, show_transcripts
):
    logger.info("Starting analysis run")
    settings_response = {
        "model": model_response,
        "temperature": float(temperature_response),
        "openai_api_key": openai_api_key,
        "anthropic_api_key": anthropic_api_key,
        "gemini_api_key": gemini_api_key
    }

    total_futures = number_of_iterations * len(chat_data)

    progress_bar = st.progress(0)
    progress_text = st.empty()

    results = []
    errors = []

    # Prepare arguments for all iterations
    all_args = []
    for chat_index, chat_info in enumerate(chat_data, start=1):
        for i in range(number_of_iterations):
            args = (
                i,
                chat_index,
                chat_info,
                settings_response,
                temperature_rating,
                model_rating,
                analyze_length,
                analyze_rating
            )
            all_args.append(args)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_chat_index = {}
        for args in all_args:
            future = executor.submit(run_single_iteration, args)
            future_to_chat_index[future] = args[1]

        completed = 0
        for future in as_completed(future_to_chat_index):
            chat_index = future_to_chat_index[future]
            result = future.result()
            if result["error"]:
                errors.append({
                    "chat_index": chat_index,
                    "iteration_index": result.get("iteration_index", "N/A"),
                    "error_message": result["error"]
                })
            else:
                results.append(result)
            completed += 1
            progress_bar.progress(completed / total_futures)
            progress_text.text(f"Completed {completed} of {total_futures} iterations.")

    progress_bar.empty()
    progress_text.empty()

    if errors:
        error_messages = ""
        for error in errors:
            error_messages += f"**Chat {error['chat_index']} Iteration {error['iteration_index']}**: {error['error_message']}\n\n"
        st.error(f"Some iterations failed due to errors:\n\n{error_messages}")
        return  # Exit the function to prevent further processing

    st.success("Analysis complete!", icon="✅")

    # Proceed with analysis only if there are successful results
    if not results:
        st.error("No successful results to analyze.")
        return

    # Organize results by chat for comparative analysis
    chat_results = {}
    for res in results:
        chat_index = res["chat_index"]
        if chat_index not in chat_results:
            chat_results[chat_index] = {
                "responses": [],
                "lengths": [],
                "ratings": [],
                "rating_texts": [],
                "total_cost": 0.0,
                "messages_per_iteration": []
            }
        chat_results[chat_index]["responses"].append(res["response"])
        chat_results[chat_index]["lengths"].append(res["length"])
        chat_results[chat_index]["ratings"].append(res["rating"])
        chat_results[chat_index]["rating_texts"].append(res["rating_text"])
        chat_results[chat_index]["total_cost"] += res["cost"]
        chat_results[chat_index]["messages_per_iteration"].append(res["messages"])

    # Generate analysis data and plots
    analysis_data, plot_base64_list, total_cost = generate_analysis(
        chat_results,
        analyze_rating,
        analyze_length
    )

    # Prepare evaluation rubrics per chat
    evaluation_rubrics = {
        chat_index: chat_info["rating_prompt_template"]
        for chat_index, chat_info in enumerate(chat_data, start=1)
        if analyze_rating and chat_info["rating_prompt_template"]
    }

    # Generate the HTML report with comparative analysis
    html_report = create_html_report(
        analysis_data,
        plot_base64_list,
        total_cost,
        chat_data=chat_data,
        chat_results=chat_results,
        model_response=model_response,
        model_rating=model_rating,
        temperature_response=temperature_response,
        temperature_rating=temperature_rating,
        evaluation_rubrics=evaluation_rubrics,
        analyze_rating=analyze_rating,
        show_transcripts=show_transcripts,
    )

    # Generate the XLSX data with results
    xlsx_with_results = generate_experiment_xlsx(
        settings_dict={
            'number_of_iterations': st.session_state.get('number_of_iterations', 3),
            'model_response': st.session_state.get('model_response', "gpt-4o-mini"),
            'temperature_response': st.session_state.get('temperature_response', 1.0),
            'model_rating': st.session_state.get('model_rating', "gpt-4o-mini"),
            'temperature_rating': st.session_state.get('temperature_rating', 0.0),
            'analyze_rating': st.session_state.get('analyze_rating', True),
            'analyze_length': st.session_state.get('analyze_length', False),
            'show_transcripts': st.session_state.get('show_transcripts', True),
            '10x_iterations': st.session_state.get('10x_iterations', False)
        },
        chat_data=chat_data,
        analysis_data=analysis_data,
        chat_results=chat_results,
        plot_base64_list=plot_base64_list
    )

    # Store results in session state
    st.session_state['analysis_results'] = {
        'html_report': html_report,
        'xlsx_with_results': xlsx_with_results
    }

def display_analysis_results():
    if 'analysis_results' in st.session_state:
        # Download button for the HTML report
        st.download_button(
            label="Download Report as HTML",
            data=st.session_state['analysis_results']['html_report'],
            file_name="analysis_report.html",
            mime="text/html"
        )

        # Download button for the Experiment as XLSX
        st.download_button(
            label="Download Experiment as XLSX",
            data=st.session_state['analysis_results']['xlsx_with_results'],
            file_name="experiment_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        # Display the HTML report in Streamlit
        st.components.v1.html(st.session_state['analysis_results']['html_report'], height=1000, scrolling=True)

# Create three columns for "Run Analysis", "Reset", and "Add Chat" buttons
col1, col2, col3 = st.columns([3, .5, .5])  # Adjust the width ratios as needed

def reset_app():
    for key in st.session_state.keys():
        del st.session_state[key]
    st.rerun()

with col1:
    if st.button("Run Analysis", key="run_analysis_button", type="primary"):
        has_empty_prompt = False

        # Check all chats for empty prompts
        for chat_index in range(1, st.session_state.num_chats + 1):
            prompt_count = st.session_state.get(f'prompt_count_chat_{chat_index}', 1)
            for i in range(1, prompt_count + 1):
                user_msg = st.session_state.get(f'user_msg_chat_{chat_index}_{i}', '').strip()
                if not user_msg:
                    has_empty_prompt = True
                    break
            if has_empty_prompt:
                break

        # --- System message validation for o1 models ---
        model_response = st.session_state.get('model_response', "gpt-4o-mini")
        model_supports_system_message = not model_response.startswith("o1")

        if not model_supports_system_message:
            # Check if any system message is non-empty
            any_system_message = any(
                st.session_state.get(f'system_msg_chat_{i}', '').strip()
                for i in range(1, st.session_state.num_chats + 1)
            )
            if any_system_message:
                st.error(
                    "The o1 model you have chosen doesn't support a system message. "
                    "Delete the system message or choose a different model."
                )
                st.stop()  # Prevent further execution

        if has_empty_prompt:
            st.error("All prompt fields must contain text. Please fill in any empty prompts.")
        else:
            with st.spinner("Running analysis..."):
                base_iterations = st.session_state.get('number_of_iterations', 3)
                if st.session_state.get('10x_iterations', False):
                    number_of_iterations = base_iterations * 10
                else:
                    number_of_iterations = base_iterations

            run_analysis(
                openai_api_key=st.session_state.get('openai_api_key', ""),
                anthropic_api_key=st.session_state.get('anthropic_api_key', ""),
                gemini_api_key=st.session_state.get('gemini_api_key', ""),
                chat_data=chat_data,
                number_of_iterations=number_of_iterations,
                model_response=st.session_state.get('model_response', "gpt-4o-mini"),
                temperature_response=st.session_state.get('temperature_response', 1.0),
                model_rating=st.session_state.get('model_rating', "gpt-4o-mini"),
                temperature_rating=st.session_state.get('temperature_rating', 0.0),
                analyze_rating=st.session_state.get('analyze_rating', True),
                analyze_length=st.session_state.get('analyze_length', False),
                show_transcripts=st.session_state.get('show_transcripts', True)
            )

with col2:
    if st.button("Reset", key="reset_button"):
        reset_app()

def add_chat():
    st.session_state.num_chats += 1
    st.session_state[f'prompt_count_chat_{st.session_state.num_chats}'] = 1

with col3:
    st.button("Add Chat ➡️", key='add_chat_button', on_click=add_chat)

# Download buttons require the file to exist when they're created, so we have to pre-emptively generate the file
if 'chat_data' in locals():
    xlsx_data = generate_settings_xlsx(
        {
            'number_of_iterations': st.session_state.get('number_of_iterations', 3),
            'model_response': st.session_state.get('model_response', "gpt-4o-mini"),
            'temperature_response': st.session_state.get('temperature_response', 1.0),
            'model_rating': st.session_state.get('model_rating', "gpt-4o-mini"),
            'temperature_rating': st.session_state.get('temperature_rating', 0.0),
            'analyze_rating': st.session_state.get('analyze_rating', True),
            'analyze_length': st.session_state.get('analyze_length', False),
            'show_transcripts': st.session_state.get('show_transcripts', True),
            '10x_iterations': st.session_state.get('10x_iterations', False)
        },
        chat_data=chat_data,
        validate_chat=False
    )
    st.sidebar.download_button(
        label="Save",
        data=xlsx_data,
        file_name="experiment_settings.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# Always display analysis results if they exist
display_analysis_results()