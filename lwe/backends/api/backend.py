import copy

from lwe.core.backend import Backend
from lwe.core.provider_manager import ProviderManager
from lwe.core.workflow_manager import WorkflowManager
from lwe.core.function_manager import FunctionManager
from lwe.core.plugin_manager import PluginManager
import lwe.core.constants as constants
import lwe.core.util as util
from lwe.backends.api.request import ApiRequest
from lwe.backends.api.conversation_storage_manager import ConversationStorageManager
from lwe.backends.api.user import UserManager
from lwe.backends.api.conversation import ConversationManager
from lwe.backends.api.message import MessageManager
from lwe.backends.orm import Conversation
from lwe.core.preset_manager import parse_llm_dict

ADDITIONAL_PLUGINS = [
    'provider_chat_openai',
]

class ApiBackend(Backend):
    """Backend implementation using direct API access.
    """

    name = "api"

    def __init__(self, config=None):
        """
        Initializes the Backend instance.

        This method sets up attributes that should only be initialized once.

        :param config: Optional configuration for the backend. If not provided, it uses a default configuration.
        """
        super().__init__(config)
        self.current_user = None
        self.user_manager = UserManager(config)
        self.conversation = ConversationManager(config)
        self.message = MessageManager(config)
        self.initialize_backend(config)

    def initialize_backend(self, config=None):
        """
        Initializes the backend with provided or default configuration,
        and sets up necessary attributes.

        This method is safe to call for dynamically reloading backends.

        :param config: Backend configuration options
        :type config: dict, optional
        """
        super().initialize_backend(config)
        self.return_only = False
        self.plugin_manager = PluginManager(self.config, self, additional_plugins=ADDITIONAL_PLUGINS)
        self.provider_manager = ProviderManager(self.config, self.plugin_manager)
        self.workflow_manager = WorkflowManager(self.config)
        self.function_manager = FunctionManager(self.config)
        self.workflow_manager.load_workflows()
        self.init_provider()
        self.set_available_models()
        self.set_conversation_tokens(0)
        self.load_default_user()
        self.load_default_conversation()

    def load_default_user(self):
        default_user = self.config.get('backend_options.default_user')
        if default_user is not None:
            self.load_user(default_user)

    def load_default_conversation(self):
        default_conversation_id = self.config.get('backend_options.default_conversation_id')
        if default_conversation_id is not None:
            self.load_conversation(default_conversation_id)

    def load_user(self, identifier):
        """Load a user by id or username/email.

        :param identifier: User id or username/email
        :type identifier: int, str
        :raises Exception: If user not found
        """
        if isinstance(identifier, int):
            success, user, user_message = self.user_manager.get_by_user_id(identifier)
        else:
            success, user, user_message = self.user_manager.get_by_username_or_email(identifier)
        if not success or not user:
            raise Exception(user_message)
        self.set_current_user(user)

    def load_conversation(self, conversation_id):
        """
        Load a conversation by id.

        :param conversation_id: Conversation id
        :type conversation_id: int
        """
        success, conversation_data, user_message = self.get_conversation(conversation_id)
        if success:
            if conversation_data:
                self.switch_to_conversation(conversation_id)
                return
            else:
                user_message = "Missing conversation data"
        raise Exception(user_message)

    def init_system_message(self):
        """Initialize the system message from config."""
        success, _alias, user_message = self.set_system_message(self.config.get('model.default_system_message'))
        if not success:
            util.print_status_message(success, user_message)
            self.set_system_message()

    def get_providers(self):
        """Get available provider plugins."""
        return self.provider_manager.get_provider_plugins()

    def init_provider(self):
        """Initialize the default provider and model."""
        self.init_system_message()
        self.active_preset = None
        self.active_preset_name = None
        default_preset = self.config.get('model.default_preset')
        if default_preset:
            success, preset, user_message = self.activate_preset(default_preset)
            if success:
                return
            util.print_status_message(False, f"Failed to load default preset {default_preset}: {user_message}")
        self.set_provider('provider_chat_openai')

    def set_provider(self, provider_name, customizations=None, reset=False):
        """
        Set the active provider plugin.

        :param provider_name: Name of provider plugin
        :type provider_name: str
        :param customizations: Customizations for provider, defaults to None
        :type customizations: dict, optional
        :param reset: Whether to reset provider, defaults to False
        :type reset: bool, optional
        :returns: success, provider, message
        :rtype: tuple
        """
        self.log.debug(f"Setting provider to: {provider_name}, with customizations: {customizations}, reset: {reset}")
        self.active_preset = None
        self.active_preset_name = None
        provider_full_name = self.provider_manager.full_name(provider_name)
        if self.provider_name == provider_full_name and not reset:
            return False, None, f"Provider {provider_name} already set"
        success, provider, user_message = self.provider_manager.load_provider(provider_full_name)
        if success:
            provider.setup()
            self.provider_name = provider_full_name
            self.provider = provider
            if isinstance(customizations, dict):
                for key, value in customizations.items():
                    success, customizations, customization_message = self.provider.set_customization_value(key, value)
                    if not success:
                        return success, customizations, customization_message
            self.llm = self.make_llm()
            self.set_model(getattr(self.llm, self.provider.model_property_name))
        return success, provider, user_message

    # TODO: This feels hacky, perhaps better to have a shell register itself
    # for output from the backend?
    def set_return_only(self, return_only=False):
        self.return_only = return_only

    def set_model(self, model_name):
        """
        Set the active model.

        :param model_name: Name of model
        :type model_name: str
        :returns: success, customizations, message
        :rtype: tuple
        """
        self.log.debug(f"Setting model to: {model_name}")
        success, customizations, user_message = super().set_model(model_name)
        self.set_max_submission_tokens(force=True)
        return success, customizations, user_message

    def compact_functions(self, customizations):
        """Compact expanded functions to just their name."""
        if 'model_kwargs' in customizations and 'functions' in customizations['model_kwargs']:
            customizations['model_kwargs']['functions'] = [f['name'] for f in customizations['model_kwargs']['functions']]
        return customizations

    def make_preset(self):
        """Make preset from current provider customizations."""
        metadata, customizations = parse_llm_dict(self.provider.customizations)
        customizations = self.compact_functions(customizations)
        return metadata, customizations

    def activate_preset(self, preset_name):
        """
        Activate a preset.

        :param preset_name: Name of preset
        :type preset_name: str
        :returns: success, preset, message
        :rtype: tuple
        """
        self.log.debug(f"Activating preset: {preset_name}")
        success, preset, user_message = self.preset_manager.ensure_preset(preset_name)
        if not success:
            return success, preset, user_message
        metadata, customizations = preset
        customizations = copy.deepcopy(customizations)
        success, provider, user_message = self.set_provider(metadata['provider'], customizations, reset=True)
        if success:
            self.active_preset = preset
            self.active_preset_name = preset_name
            if 'system_message' in metadata:
                self.set_system_message(metadata['system_message'])
        return success, preset, user_message

    def _handle_response(self, success, obj, message):
        """
        Handle response tuple.

        Logs errors if not successful.

        :param success: If request was successful
        :type success: bool
        :param obj: Returned object
        :param message: Message
        :type message: str
        :returns: success, obj, message
        :rtype: tuple
        """
        if not success:
            self.log.error(message)
        return success, obj, message

    def set_conversation_tokens(self, tokens):
        """
        Set current conversation token count.

        :param tokens: Number of conversation tokens
        :type tokens: int
        """
        if self.conversation_id is None:
            provider = self.provider
        else:
            success, last_message, user_message = self.message.get_last_message(self.conversation_id)
            if not success:
                raise ValueError(user_message)
            provider = self.provider_manager.get_provider_from_name(last_message['provider'])
        if provider is not None and provider.get_capability('chat'):
            self.conversation_tokens = tokens
        else:
            self.conversation_tokens = None

    def switch_to_conversation(self, conversation_id):
        """
        Switch to a conversation.

        :param conversation_id: Conversation id
        :type conversation_id: int
        """
        success, conversation, user_message = self.get_conversation(conversation_id)
        if success:
            self.conversation_id = conversation_id
            self.conversation_title = conversation['conversation']['title']
        else:
            raise ValueError(user_message)
        success, last_message, user_message = self.message.get_last_message(self.conversation_id)
        if not success:
            raise ValueError(user_message)
        model_configured = False
        if last_message['preset']:
            success, _preset, user_message = self.activate_preset(last_message['preset'])
            if success:
                model_configured = True
            else:
                util.print_status_message(False, f"Unable to switch conversation to previous preset '{last_message['preset']}' -- ERROR: {user_message}, falling back to provider: {last_message['provider']}, model: {last_message['model']}")
        if not model_configured:
            if last_message['provider'] and last_message['model']:
                success, _provider, _user_message = self.set_provider(last_message['provider'], reset=True)
                if success:
                    success, _customizations, _user_message = self.set_model(last_message['model'])
                    if success:
                        self.init_system_message()
                        model_configured = True
        if not model_configured:
            util.print_status_message(False, "Invalid conversation provider/model, falling back to default provider/model")
            self.init_provider()
        conversation_storage_manager = ConversationStorageManager(self.config,
                                                                  self.function_manager,
                                                                  self.current_user,
                                                                  self.conversation_id,
                                                                  self.provider,
                                                                  self.model,
                                                                  self.active_preset_name or '',
                                                                  )
        tokens = conversation_storage_manager.get_conversation_token_count()
        self.set_conversation_tokens(tokens)

    def get_system_message(self, system_message='default'):
        """
        Get the system message.

        :param system_message: System message alias
        :type system_message: str
        :returns: System message
        :rtype: str
        """
        aliases = self.get_system_message_aliases()
        if system_message in aliases:
            system_message = aliases[system_message]
        return system_message

    def set_system_message(self, system_message='default'):
        """
        Set the system message.

        :param system_message: System message or alias
        :type system_message: str
        """
        self.system_message = self.get_system_message(system_message)
        self.system_message_alias = system_message if system_message in self.get_system_message_aliases() else None
        message = f"System message set to: {self.system_message}"
        self.log.info(message)
        return True, system_message, message

    def set_max_submission_tokens(self, max_submission_tokens=None, force=False):
        """
        Set the max submission tokens.

        :param max_submission_tokens: Max submission tokens
        :type max_submission_tokens: int
        :param force: Force setting max submission tokens
        :type force: bool
        """
        chat = self.provider.get_capability('chat')
        if chat or force:
            self.max_submission_tokens = max_submission_tokens or self.provider.max_submission_tokens()
            return True, self.max_submission_tokens, f"Max submission tokens set to {self.max_submission_tokens}"
        return False, None, "Setting max submission tokens not supported for this provider"

    def get_runtime_config(self):
        """
        Get the runtime configuration.

        :returns: Runtime configuration
        :rtype: str
        """
        output = """
* Max submission tokens: %s
* System message: %s
""" % (self.max_submission_tokens, self.system_message)
        return output

    def get_system_message_aliases(self):
        """
        Get system message aliases from config.

        :returns: Dict of message aliases
        :rtype: dict
        """
        aliases = self.config.get('model.system_message')
        aliases['default'] = constants.SYSTEM_MESSAGE_DEFAULT
        return aliases

    def retrieve_old_messages(self, conversation_id=None, target_id=None):
        """
        Retrieve old messages for a conversation.

        :param conversation_id: Conversation id, defaults to current
        :type conversation_id: int, optional
        :param target_id: Target message id, defaults to None
        :type target_id: int, optional
        :returns: List of messages
        :rtype: list
        """
        old_messages = []
        if conversation_id:
            success, old_messages, message = self.message.get_messages(conversation_id, target_id=target_id)
            if not success:
                raise Exception(message)
        return old_messages

    def set_current_user(self, user=None):
        """
        Set the current user.

        :param user: User object, defaults to None
        :type user: User, optional
        :returns: success, preset, message on preset activation, otherwise init the provider
        :rtype: tuple
        """
        self.log.debug(f"Setting current user to {user.username if user else None}")
        self.current_user = user
        if self.current_user:
            if self.current_user.default_preset:
                self.log.debug(f"Activating user default preset: {self.current_user.default_preset}")
                return self.activate_preset(self.current_user.default_preset)
        return self.init_provider()

    def conversation_data_to_messages(self, conversation_data):
        """
        Convert conversation data to list of messages.

        :param conversation_data: Conversation data dict
        :type conversation_data: dict
        :returns: List of messages
        :rtype: list
        """
        return conversation_data['messages']

    def delete_conversation(self, conversation_id=None):
        """Delete a conversation.

        :param conversation_id: Conversation id, defaults to current
        :type conversation_id: int, optional
        :returns: success, conversation, message
        :rtype: tuple
        """
        conversation_id = conversation_id if conversation_id else self.conversation_id
        success, conversation, message = self.conversation.delete_conversation(conversation_id)
        return self._handle_response(success, conversation, message)

    def set_title(self, title, conversation_id=None):
        """
        Set conversation title.

        :param title: New title
        :type title: str
        :param conversation_id: Conversation id, defaults to current
        :type conversation_id: int, optional
        :returns: success, conversation, message
        :rtype: tuple
        """
        conversation_id = conversation_id if conversation_id else self.conversation_id
        success, conversation, user_message = self.conversation.edit_conversation_title(conversation_id, title)
        if success:
            self.conversation_title = conversation.title
        return self._handle_response(success, conversation, user_message)

    def get_history(self, limit=20, offset=0, user_id=None):
        """
        Get conversation history.

        :param limit: Number of results, defaults to 20
        :type limit: int, optional
        :param offset: Result offset, defaults to 0
        :type offset: int, optional
        :param user_id: User id, defaults to current
        :type user_id: int, optional
        :returns: success, history dict, message
        :rtype: tuple
        """
        user_id = user_id if user_id else self.current_user.id
        success, conversations, message = self.conversation.get_conversations(user_id, limit=limit, offset=offset)
        if success:
            history = {m.id: self.conversation.orm.object_as_dict(m) for m in conversations}
            return success, history, message
        return self._handle_response(success, conversations, message)

    def get_conversation(self, id=None):
        """
        Get a conversation.

        :param id: Conversation id, defaults to current
        :type id: int, optional
        :returns: success, conversation dict, message
        :rtype: tuple
        """
        id = id if id else self.conversation_id
        if not id:
            return False, None, "No current conversation"
        success, conversation, message = self.conversation.get_conversation(id)
        if success:
            success, messages, message = self.message.get_messages(id)
            if success:
                conversation_data = {
                    "conversation": self.conversation.orm.object_as_dict(conversation),
                    "messages": messages,
                }
                return success, conversation_data, message
        return self._handle_response(success, conversation, message)

    def get_current_conversation_title(self):
        if not self.conversation_id:
            return None
        if self.conversation_title:
            return self.conversation_title
        success, conversation, message = self.conversation.get_conversation(self.conversation_id)
        return success and conversation.id or None

    def new_conversation(self):
        """Start a new conversation."""
        super().new_conversation()
        self.set_conversation_tokens(0)

    def make_request(self, input, request_overrides: dict = None):
        """
        Ask the LLM a question, return and optionally stream a response.

        :param input: The input to be sent to the LLM.
        :type input: str
        :request_overrides: Overrides for this specific request.
        :type request_overrides: dict, optional
        :returns: success, LLM response, message
        :rtype: tuple
        """
        self.log.info("Starting 'ask' request")
        request_overrides = request_overrides or {}
        old_messages = self.retrieve_old_messages(self.conversation_id)
        self.log.debug(f"Extracting activate preset configuration from request_overrides: {self.request_overrides}")
        success, response, user_message = util.extract_preset_configuration_from_request_overrides(self.request_overrides, self.active_preset_name)
        if not success:
            return success, response, user_message
        preset_name, _preset_overrides, activate_preset = response
        request = ApiRequest(self.config,
                             self.provider,
                             self.function_manager,
                             input,
                             self.active_preset,
                             self.preset_manager,
                             self.system_message,
                             old_messages,
                             self.conversation_tokens,
                             self.max_submission_tokens,
                             request_overrides,
                             )
        request.set_request_llm()
        new_messages, messages = request.prepare_ask_request()
        success, response_obj, user_message = request.call_llm(messages)
        if success:
            response_content, new_messages = request.post_response(response_obj, new_messages)
            self.message_clipboard = response_content
            title = request_overrides.get('title')
            conversation_storage_manager = ConversationStorageManager(self.config,
                                                                      self.function_manager,
                                                                      self.current_user,
                                                                      self.conversation_id,
                                                                      request.provider,
                                                                      request.model_name,
                                                                      request.preset_name,
                                                                      )
            success, response_obj, user_message = conversation_storage_manager.store_conversation_messages(new_messages, response_content, title)
            if success:
                if isinstance(response_obj, Conversation):
                    conversation = response_obj
                    self.conversation_id = conversation.id
                    self.conversation_title = conversation.title
                    tokens = conversation_storage_manager.get_conversation_token_count()
                    self.set_conversation_tokens(tokens)
                response_obj = response_content
                if activate_preset:
                    self.log.info(f"Activating preset from request override: {preset_name}")
                    self.activate_preset(preset_name)
        return self._handle_response(success, response_obj, user_message)

    def ask_stream(self, input: str, request_overrides: dict = None):
        """
        Ask the LLM a question and stream a response.

        :param input: The input to be sent to the LLM.
        :type input: str
        :request_overrides: Overrides for this specific request.
        :type request_overrides: dict, optional
        :returns: success, LLM response, message
        :rtype: tuple
        """
        request_overrides = request_overrides or {}
        request_overrides['stream'] = True
        return self._ask(input, request_overrides)

    def ask(self, input: str, request_overrides: dict = None):
        """
        Ask the LLM a question and return response.

        :param input: The input to be sent to the LLM.
        :type input: str
        :request_overrides: Overrides for this specific request.
        :type request_overrides: dict, optional
        :returns: success, LLM response, message
        :rtype: tuple
        """
        return self._ask(input, request_overrides)
