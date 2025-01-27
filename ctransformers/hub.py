import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.utils import validate_repo_id, HFValidationError

from .llm import Config, LLM


def get_path_type(path: str) -> Optional[str]:
    p = Path(path)
    if p.is_file():
        return "file"
    elif p.is_dir():
        return "dir"
    try:
        validate_repo_id(path)
        return "repo"
    except HFValidationError:
        pass


@dataclass
class AutoConfig:
    config: Config
    model_type: Optional[str] = None

    @classmethod
    def from_pretrained(
        cls,
        model_path_or_repo_id: str,
        local_files_only: bool = False,
        **kwargs,
    ) -> "AutoConfig":
        path_type = get_path_type(model_path_or_repo_id)
        if not path_type:
            raise ValueError(f"Model path '{model_path_or_repo_id}' doesn't exist.")

        config = Config()
        auto_config = AutoConfig(config=config)

        if path_type == "dir":
            cls._update_from_dir(model_path_or_repo_id, auto_config)
        elif path_type == "repo":
            cls._update_from_repo(
                model_path_or_repo_id,
                auto_config,
                local_files_only=local_files_only,
            )

        for k, v in kwargs.items():
            if not hasattr(config, k):
                raise TypeError(
                    f"'{k}' is an invalid keyword argument for from_pretrained()"
                )
            setattr(config, k, v)

        return auto_config

    @classmethod
    def _update_from_repo(
        cls,
        repo_id: str,
        auto_config: "AutoConfig",
        local_files_only: bool,
    ) -> None:
        path = snapshot_download(
            repo_id=repo_id,
            allow_patterns="config.json",
            local_files_only=local_files_only,
        )
        cls._update_from_dir(path, auto_config)

    @classmethod
    def _update_from_dir(cls, path: str, auto_config: "AutoConfig") -> None:
        path = (Path(path) / "config.json").resolve()
        if path.is_file():
            cls._update_from_file(path, auto_config)

    @classmethod
    def _update_from_file(cls, path: str, auto_config: "AutoConfig") -> None:
        with open(path) as f:
            config = json.load(f)

        auto_config.model_type = config.get("model_type")
        params = config.get("task_specific_params", {})
        params = params.get("text-generation", {})
        for name in [
            "top_k",
            "top_p",
            "temperature",
            "repetition_penalty",
            "last_n_tokens",
        ]:
            value = params.get(name)
            if value is not None:
                setattr(auto_config.config, name, value)


class AutoModelForCausalLM:
    @classmethod
    def from_pretrained(
        cls,
        model_path_or_repo_id: str,
        *,
        model_type: Optional[str] = None,
        model_file: Optional[str] = None,
        config: Optional[AutoConfig] = None,
        lib: Optional[str] = None,
        local_files_only: bool = False,
        **kwargs,
    ) -> LLM:
        """Loads the language model from a local file or remote repo.

        Args:
            model_path_or_repo_id: The path to a model file or directory or the
            name of a Hugging Face Hub model repo.
            model_type: The model type.
            model_file: The name of the model file in repo or directory.
            config: `AutoConfig` object.
            lib: The path to a shared library or one of `avx2`, `avx`, `basic`.
            local_files_only: Whether or not to only look at local files
            (i.e., do not try to download the model).

        Returns:
            `LLM` object.
        """
        if model_type is None and "gptq" in str(model_path_or_repo_id).lower():
            model_type = "gptq"
        if model_type == "gptq":
            from . import gptq

            return gptq.AutoModelForCausalLM.from_pretrained(
                model_path_or_repo_id,
                local_files_only=local_files_only,
                **kwargs,
            )

        config = config or AutoConfig.from_pretrained(
            model_path_or_repo_id,
            local_files_only=local_files_only,
            **kwargs,
        )
        model_type = model_type or config.model_type
        if not model_type:
            raise ValueError(
                "Unable to detect model type. Please specify a model type using:\n\n"
                "  AutoModelForCausalLM.from_pretrained(..., model_type='...')\n\n"
            )

        path_type = get_path_type(model_path_or_repo_id)
        model_path = None
        if path_type == "file":
            model_path = model_path_or_repo_id
        elif path_type == "dir":
            model_path = cls._find_model_path_from_dir(
                model_path_or_repo_id, model_file
            )
        elif path_type == "repo":
            model_path = cls._find_model_path_from_repo(
                model_path_or_repo_id,
                model_file,
                local_files_only=local_files_only,
            )

        return LLM(
            model_path=model_path,
            model_type=model_type,
            config=config.config,
            lib=lib,
        )

    @classmethod
    def _find_model_path_from_repo(
        cls,
        repo_id: str,
        filename: Optional[str],
        local_files_only: bool,
    ) -> str:
        if not filename and not local_files_only:
            filename = cls._find_model_file_from_repo(repo_id=repo_id)
        allow_patterns = filename or "*.bin"
        path = snapshot_download(
            repo_id=repo_id,
            allow_patterns=allow_patterns,
            local_files_only=local_files_only,
        )
        return cls._find_model_path_from_dir(path, filename=filename)

    @classmethod
    def _find_model_file_from_repo(cls, repo_id: str) -> Optional[str]:
        api = HfApi()
        repo_info = api.repo_info(repo_id=repo_id, files_metadata=True)
        files = [
            (f.size, f.rfilename)
            for f in repo_info.siblings
            if f.rfilename.endswith(".bin")
        ]
        if not files:
            raise ValueError(f"No model file found in repo '{repo_id}'")
        return min(files)[1]

    @classmethod
    def _find_model_path_from_dir(
        cls,
        path: str,
        filename: Optional[str] = None,
    ) -> str:
        path = Path(path).resolve()
        if filename:
            file = (path / filename).resolve()
            if not file.is_file():
                raise ValueError(f"Model file '{filename}' not found in '{path}'")
            return str(file)

        files = [
            (f.stat().st_size, f)
            for f in path.iterdir()
            if f.is_file() and f.name.endswith(".bin")
        ]
        if not files:
            raise ValueError(f"No model file found in directory '{path}'")
        file = min(files)[1]
        return str(file.resolve())
