import typing as t

import instructor
import litellm
from mistralai import Mistral

from bespokelabs.curator.log import logger
from bespokelabs.curator.request_processor.batch.base_batch_request_processor import BaseBatchRequestProcessor
from bespokelabs.curator.request_processor.config import BatchRequestProcessorConfig
from bespokelabs.curator.types.generic_batch import GenericBatch, GenericBatchRequestCounts, GenericBatchStatus
from bespokelabs.curator.types.generic_request import GenericRequest
from bespokelabs.curator.types.generic_response import GenericResponse
from bespokelabs.curator.types.token_usage import _TokenUsage

# Reference for Mistral status: https://github.com/mistralai/client-python/blob/main/docs/models/batchjobstatus.md
_PROGRESS_STATE = {"QUEUED", "RUNNING", "CANCELLATION_REQUESTED"}
_FINISHED_STATE = {"SUCCESS", "FAILED", "TIMEOUT_EXCEEDED", "CANCELLED"}


class MistralBatchRequestProcessor(BaseBatchRequestProcessor):
    """Mistral-specific implementation of the BatchRequestProcessor.

    This class handles batch processing of requests using Mistral's API, including
    file uploads, batch submissions, and result retrieval.
    """

    def __init__(self, config: BatchRequestProcessorConfig) -> None:
        """Initialize the MistralBatchRequestProcessor."""
        super().__init__(config)
        self.client = Mistral(api_key=config.api_key)

    @property
    def backend(self):
        """Backend property."""
        return "mistral"

    @property
    def max_requests_per_batch(self) -> int:
        """The maximum number of requests that can be processed in a batch."""
        return 1000000  # 1 million as per Mistral's documentation - https://docs.mistral.ai/capabilities/batch/

    @property
    def max_bytes_per_batch(self) -> int:
        """The maximum number of bytes that can be processed in a batch."""
        return 100 * 1024 * 1024  # NOTE Example limit (100MB), adjust based on Mistral's documentation (https://mistral.ai/products/la-plateforme#pricing)

    @property
    def max_concurrent_batch_operations(self) -> int:
        """The maximum number of concurrent batch operations."""
        return 100  # NOTE Example limit, adjust based on Mistral's documentation (https://mistral.ai/products/la-plateforme#pricing)

    def parse_api_specific_request_counts(
        self, mistral_batch_object: t.Any
    ) -> GenericBatchRequestCounts:  # TODO: Replace t.Any with the correct type (idea: Should I create a pydantic model for Mistral's Batch object?)
        """Convert Mistral-specific request counts to generic format.

        Handles the following Mistral request count statuses:
        - completed: Successfully completed requests
        - failed: Requests that failed
        - total: Total number of requests in batch

        Args:
            mistral_batch_object: Mistral batch object.

        Returns:
            GenericBatchRequestCounts: Standardized request count format.

        """
        return GenericBatchRequestCounts(
            failed=mistral_batch_object.failed_requests,
            succeeded=mistral_batch_object.succeeded_requests,
            total=mistral_batch_object.total_requests,
            raw_request_counts_object=mistral_batch_object.model_dump(),
        )

    def parse_api_specific_batch_object(self, mistral_batch_object: t.Any, request_file: str | None = None) -> GenericBatch:
        """Convert a Mistral batch object to generic format.

        Convert Mistral-specific request counts to generic format.

        Maps Mistral-specific batch statuses and timing information to our
        standardized GenericBatch format.

        Batch statuses:
        - QUEUED, RUNNING, CANCELLATION_REQUESTED, CANCELLATION_REQUESTED: Mapped to SUBMITTED
        - SUCCESS, FAILED, TIMEOUT_EXCEEDED, CANCELLED: Mapped to FINISHED

        Timing fields:
        - created_at: When the batch was created
        - completed_at: When processing ended
        - # NOTE: Mistral client-python also as a parameter `started_at` which is not used in this function

        Args:
            mistral_batch_object: Mistral's Batch object.
            request_file: Optional path to the request file.

        Returns:
            GenericBatch: Standardized batch object.

        Reference: Mistral client-python: https://github.com/mistralai/client-python/blob/main/docs/models/batchjobout.md
        """
        if mistral_batch_object.status in _PROGRESS_STATE:
            status = GenericBatchStatus.SUBMITTED
        elif mistral_batch_object.status in _FINISHED_STATE:
            status = GenericBatchStatus.FINISHED
        else:
            raise ValueError(f"Unknown batch status: {mistral_batch_object.status}")

        return GenericBatch(
            request_file=request_file,
            id=mistral_batch_object.id,
            created_at=mistral_batch_object.created_at,
            finished_at=mistral_batch_object.completed_at,
            status=status,
            api_key_suffix=self.client.api_key[-4:],
            raw_batch=mistral_batch_object.model_dump(),
            request_counts=self.parse_api_specific_request_counts(mistral_batch_object.total_requests),
            raw_status=mistral_batch_object.status,
        )

    def parse_api_specific_response(self, raw_response: dict, generic_request: GenericRequest, batch: GenericBatch) -> GenericResponse:
        """Parse Mistral API response into generic format.

        Processes raw responses from Mistral's batch API, handling both successful
        and failed responses. For successful responses, calculates token usage
        and applies batch pricing discount.

        Args:
            raw_response: Raw response dictionary from Mistral's API.
            generic_request: Original generic request object.
            batch: The batch object containing timing information.

        Returns:
            GenericResponse: Standardized response object with parsed message,
                errors, token usage, and cost information.

        Side Effects: # TODO: Add side effects
            - Calculates costs with 50% batch discount
            - Handles failed requests with error details

        Reference: Mistral API request/response format: https://docs.mistral.ai/api/#tag/chat
        """
        if raw_response.status_code != 200:
            response_message = None
            response_errors = raw_response["detail"]["msg"]
            token_usage = None
            cost = None
        else:
            response_message = raw_response["choices"][0]["message"]["content"]
            response_errors = None
            token_usage = _TokenUsage(
                prompt_tokens=raw_response["usage"]["prompt_tokens"],
                completion_tokens=raw_response["usage"]["completion_tokens"],
                total_tokens=raw_response["usage"]["total_tokens"],
            )
            cost = raw_response["metadata"]["cost"]
            # TODO: Mistral chat-completions doesnt have cost as per docs (though for fine-tning it does have a `cost` key in metadata): (https://docs.mistral.ai/api/#tag/chat)

        return GenericResponse(
            response_message=response_message,
            response_errors=response_errors,
            raw_response=raw_response,
            raw_request=None,
            generic_request=generic_request,
            created_at=batch.created_at,
            finished_at=batch.finished_at,
            token_usage=token_usage,
            response_cost=cost,
        )

    def create_api_specific_request_batch(self, generic_request: GenericRequest) -> dict:
        """Creates an API-specific request body from a generic request body.

        Transforms a GenericRequest into the format expected by MIstral's batch API.

        Args:
            generic_request: Generic request object containing model, messages,
            and optional response format.

        Returns:
            dict: API-specific request body for Mistral's batch API includes:
            - custom_id: Original row index of the request.
            - body: dictionary containing max_token, messages and other parameters.

        Reference: Mistral batch API documentation: https://docs.mistral.ai/capabilities/batch/
        """
        _, kwargs = instructor.handle_response_model(
            self.prompt_formatter.response_format,
            mode=instructor.Mode.JSON,
            messages=generic_request.messages,
        )
        request = {
            "custom_id": str(generic_request.original_row_idx),
            "body": {
                "max_tokens": litellm.get_max_tokens(self.config.model),
                "messages": generic_request.messages,
                **kwargs,  # contains 'system' and 'messages'
                **generic_request.generation_params,
            },
        }
        return request

    async def upload_batch_file(self, file_content: bytes) -> t.Any:  # TODO: Define the return type
        """Uploads a batch file to OpenAI and waits until ready.

        Args:
            file_content (bytes): The encoded file content to upload

        Returns:
            str: The uploaded file object from OpenAI
        """
        batch_file_upload = await self.client.upload(
            file={
                "filename": "None",  # TODO: Add filename
                "content": file_content,
            },
            purpose="batch",
        )
        return batch_file_upload

    async def create_batch(self, batch_file_id: str, metadata: dict) -> t.Any:
        """Creates a batch job with Mistral using an uploaded file.

        Args:
            batch_file_id (str): ID of the uploaded file to use for the batch
            metadata (dict): Metadata to be included with the batch

        Returns:
            Batch: The created batch object from OpenAI

        Raises:
            Exception: If batch creation fails

        Reference: Mistral batch API documentation (batch processing full example): https://docs.mistral.ai/capabilities/batch/#tag/ocr/operation/ocr_v1_ocr_post
        """
        try:
            batch = await self.client.batch.jobs.create(
                input_files=batch_file_id,
                model=self.config.model,
                endpoint="/v1/chat/completions",
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"Error submitting batch: {e}")
            raise e
        return batch

    async def submit_batch(self, requests: list[dict], metadata: dict) -> GenericBatch:
        """Handles the complete batch submission process.

        Args:
            requests (list[dict]): List of API-specific requests to submit
            metadata (dict): Metadata to be included with the batch

        Returns:
            Batch: The created batch object for Mistral

        Side Effects:
            - Updates tracker with submitted batch status
        """
        async with self.semaphore:
            file_content = self.create_batch_file(requests)
            batch_file = await self.upload_batch_file(file_content)
            batch = await self.create_batch(batch_file.id, metadata)
            return self.parse_api_specific_batch_object(batch, metadata.get("request_file"))

    async def retrieve_batch(self, batch: GenericBatch) -> GenericBatch:
        """Retrieve current status of a batch from Mistral's API.

        Args:
            batch: The batch object to retrieve status for.

        Returns:
            GenericBatch: Updated batch object with current status.
            None: If the batch is not found or inaccessible.

        Side Effects:
            - Logs error is batch retrieval fails
        """
        try:
            batch = await self.client.jobs.get(job_id=batch.id)
        except Exception as e:
            logger.error(f"Failed to retrieve batch: {e}")
            return None
        return self.parse_api_specific_batch_object(batch, request_file=batch.request_file)

    async def download_batch(self, batch: GenericBatch) -> list[dict] | None:
        """Download the results of a completed batch."""
        pass

    async def cancel_batch(self, batch: GenericBatch) -> GenericBatch:
        """Cancel a batch job."""
        pass
