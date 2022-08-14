#  Copyright (c) 2022. DataRobot, Inc. and its affiliates.
#  All rights reserved.
#  This is proprietary source code of DataRobot, Inc. and its affiliates.
#  Released under the terms of DataRobot Tool and Utility Agreement.

"""
Functional tests for the deployment GitHub action. Functional tests are executed against a running
DataRobot application. If DataRobot is not accessible, the functional tests are skipped.
"""

import contextlib
import copy
import os
from pathlib import Path

import pytest
import yaml
from bson import ObjectId

from common.exceptions import DataRobotClientError
from common.exceptions import IllegalModelDeletion
from custom_inference_deployment import DeploymentInfo
from schema_validator import DeploymentSchema
from schema_validator import ModelSchema
from tests.functional.conftest import cleanup_models
from tests.functional.conftest import increase_model_memory_by_1mb
from tests.functional.conftest import run_github_action
from tests.functional.conftest import temporarily_replace_schema_value
from tests.functional.conftest import printout
from tests.functional.conftest import temporarily_replace_schema
from tests.functional.conftest import upload_and_update_dataset
from tests.functional.conftest import webserver_accessible


@pytest.fixture
@pytest.mark.usefixtures("build_repo_for_testing")
def deployment_metadata_yaml_file(repo_root_path, git_repo, model_metadata):
    """A fixture to return a unique deployment from the temporary created local source tree."""

    deployment_yaml_file = next(repo_root_path.rglob("**/deployment.yaml"))
    with open(deployment_yaml_file) as f:
        yaml_content = yaml.safe_load(f)
        yaml_content[DeploymentSchema.DEPLOYMENT_ID_KEY] = f"deployment-id-{str(ObjectId())}"
        yaml_content[DeploymentSchema.MODEL_ID_KEY] = model_metadata[ModelSchema.MODEL_ID_KEY]

    with open(deployment_yaml_file, "w") as f:
        yaml.safe_dump(yaml_content, f)

    git_repo.git.add(deployment_yaml_file)
    git_repo.git.commit("--amend", "--no-edit")

    return deployment_yaml_file


@pytest.fixture
def deployment_metadata(deployment_metadata_yaml_file):
    """A fixture to load and return a deployment metadata from a given yaml file definition."""

    with open(deployment_metadata_yaml_file) as f:
        return yaml.safe_load(f)


@pytest.fixture
def cleanup(dr_client, repo_root_path, deployment_metadata):
    """A fixture to delete all deployments and models that were created from the source tree."""

    yield

    try:
        dr_client.delete_deployment_by_git_id(
            deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        )
    except (IllegalModelDeletion, DataRobotClientError):
        pass

    # NOTE: we have more than one model in the tree
    cleanup_models(dr_client, repo_root_path)


@pytest.mark.skipif(not webserver_accessible(), reason="DataRobot webserver is not accessible.")
@pytest.mark.usefixtures("build_repo_for_testing")
class TestDeploymentGitHubActions:
    """Contains cases to test the deployment GitHub action."""

    @contextlib.contextmanager
    def _upload_actuals_dataset(
        self, event_name, dr_client, deployment_metadata, deployment_metadata_yaml_file
    ):
        if event_name == "push":
            association_id = DeploymentSchema.get_value(
                deployment_metadata,
                DeploymentSchema.SETTINGS_SECTION_KEY,
                DeploymentSchema.ASSOCIATION_KEY,
                DeploymentSchema.ASSOCIATION_ACTUALS_ID_KEY,
            )
            if association_id:
                actuals_filepath = (
                    Path(__file__).parent
                    / ".."
                    / "datasets"
                    / "juniors_3_year_stats_regression_actuals.csv"
                )
                with upload_and_update_dataset(
                    dr_client,
                    actuals_filepath,
                    deployment_metadata_yaml_file,
                    DeploymentSchema.ASSOCIATION_KEY,
                    DeploymentSchema.ASSOCIATION_ACTUALS_DATASET_ID_KEY,
                ) as dataset_id:
                    yield dataset_id
        else:
            yield None

    @pytest.mark.parametrize("event_name", ["push", "pull_request"])
    @pytest.mark.usefixtures("cleanup", "skip_model_testing")
    def test_e2e_deployment_create(
        self,
        dr_client,
        repo_root_path,
        git_repo,
        model_metadata,
        model_metadata_yaml_file,
        deployment_metadata,
        deployment_metadata_yaml_file,
        main_branch_name,
        event_name,
    ):
        """An end-to-end case to test a deployment creation."""

        # 1. Create a model just as a preliminary requirement (use GitHub action)
        printout(
            "Create a custom model as a preliminary requirement. "
            "Run custom model GitHub action (push event) ..."
        )
        run_github_action(repo_root_path, git_repo, main_branch_name, "push", is_deploy=False)

        # 2. Upload actuals dataset and set the deployment metadata with the dataset ID
        printout("Upload actuals dataset ...")
        with self._upload_actuals_dataset(
            event_name, dr_client, deployment_metadata, deployment_metadata_yaml_file
        ):
            printout("Run deployment GitHub action ...")
            # 3. Run a deployment github action
            run_github_action(
                repo_root_path, git_repo, main_branch_name, event_name, is_deploy=True
            )

        # 4. Validate
        printout("Validate ...")
        local_git_deployment_id = deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        if event_name == "push":
            assert dr_client.fetch_deployment_by_git_id(local_git_deployment_id) is not None
        elif event_name == "pull_request":
            assert dr_client.fetch_deployment_by_git_id(local_git_deployment_id) is None
        else:
            assert False, f"Unsupported GitHub event name: {event_name}"

        printout("Done")

    @pytest.mark.parametrize("event_name", ["push", "pull_request"])
    @pytest.mark.usefixtures("cleanup", "set_model_dataset_for_testing")
    def test_e2e_deployment_model_replacement(
        self,
        dr_client,
        repo_root_path,
        git_repo,
        model_metadata_yaml_file,
        deployment_metadata,
        deployment_metadata_yaml_file,
        main_branch_name,
        event_name,
    ):
        """An end-to-end case to test a model replacement in a deployment."""

        # Disable challengers
        printout("Disable challengers ...")
        self._enable_challenger(deployment_metadata, deployment_metadata_yaml_file, False)

        (
            _,
            latest_deployment_model_version_id,
            latest_model_version,
        ) = self._deploy_a_model_than_run_github_action_to_replace_or_challenge(
            dr_client,
            git_repo,
            repo_root_path,
            main_branch_name,
            deployment_metadata,
            model_metadata_yaml_file,
            event_name,
        )

        if event_name == "push":
            assert latest_deployment_model_version_id
            assert latest_deployment_model_version_id == latest_model_version["id"]
        elif event_name == "pull_request":
            assert latest_deployment_model_version_id
            assert latest_model_version["id"]
            assert latest_deployment_model_version_id != latest_model_version["id"]
        else:
            assert False, f"Unsupported GitHub event name: {event_name}"
        printout("Done")

    @staticmethod
    def _deploy_a_model_than_run_github_action_to_replace_or_challenge(
        dr_client,
        git_repo,
        repo_root_path,
        main_branch_name,
        deployment_metadata,
        model_metadata_yaml_file,
        event_name,
    ):

        # 1. Create a model just as a preliminary requirement (use GitHub action)
        printout(
            "Create a custom model as a preliminary requirement. "
            "Run custom model GitHub action (push event) ..."
        )
        run_github_action(repo_root_path, git_repo, main_branch_name, "push", is_deploy=False)

        # 2. Create a deployment
        printout("Create a deployment. Run deployment GitHub action (push event) ...")
        run_github_action(repo_root_path, git_repo, main_branch_name, "push", is_deploy=True)
        local_git_deployment_id = deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        deployments = dr_client.fetch_deployments()
        assert any(d.get("gitDeploymentId") == local_git_deployment_id for d in deployments)

        # 3. Make a local change to the model and commit
        printout("Make a change to the model and run custom model GitHub action (push event) ...")
        new_memory = increase_model_memory_by_1mb(model_metadata_yaml_file)
        os.chdir(repo_root_path)
        git_repo.git.commit("-a", "-m", f"Increase memory to {new_memory}")

        # 4. Run GitHub action to create a new model version in DataRobot
        run_github_action(repo_root_path, git_repo, main_branch_name, "push", is_deploy=False)

        # 5. Run GitHub action to replace the latest model in a deployment
        printout(f"Run deployment GitHub action ({event_name} event) ...")
        run_github_action(repo_root_path, git_repo, main_branch_name, event_name, is_deploy=True)

        printout(f"Validate ...")
        deployments = dr_client.fetch_deployments()
        the_deployment = next(
            d for d in deployments if d.get("gitDeploymentId") == local_git_deployment_id
        )

        latest_deployment_model_version_id = the_deployment["model"]["customModelImage"][
            "customModelVersionId"
        ]
        local_git_model_id = deployment_metadata[DeploymentSchema.MODEL_ID_KEY]
        latest_model_version = dr_client.fetch_custom_model_latest_version_by_git_model_id(
            local_git_model_id
        )
        return the_deployment, latest_deployment_model_version_id, latest_model_version

    @staticmethod
    def _enable_challenger(deployment_metadata, deployment_metadata_yaml_file, enabled=True):
        settings = deployment_metadata.get(DeploymentSchema.SETTINGS_SECTION_KEY, {})
        settings[DeploymentSchema.ENABLE_CHALLENGER_MODELS_KEY] = enabled
        deployment_metadata[DeploymentSchema.SETTINGS_SECTION_KEY] = settings
        with open(deployment_metadata_yaml_file, "w") as f:
            yaml.safe_dump(deployment_metadata, f)

    @pytest.mark.usefixtures("cleanup", "skip_model_testing")
    def test_e2e_deployment_delete(
        self,
        dr_client,
        repo_root_path,
        git_repo,
        model_metadata_yaml_file,
        deployment_metadata,
        deployment_metadata_yaml_file,
        main_branch_name,
    ):
        """An end-to-end case to test a deployment deletion."""

        # 1. Create a model just as a basic requirement (use GitHub action)
        printout(
            "Create a custom model as a preliminary requirement. "
            "Run custom model GitHub action (push event) ..."
        )
        run_github_action(repo_root_path, git_repo, main_branch_name, "push", is_deploy=False)

        # 2. Run a deployment GitHub action to create a deployment
        printout("Create a deployment. Runa deployment GitHub action (push event) ...")
        run_github_action(repo_root_path, git_repo, main_branch_name, "push", is_deploy=True)
        deployments = dr_client.fetch_deployments()
        local_git_deployment_id = deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        assert any(d.get("gitDeploymentId") == local_git_deployment_id for d in deployments)

        # 3. Delete a deployment local definition yaml file
        printout("Delete deployment. Run deployment GitHub action (push event) ...")
        os.remove(deployment_metadata_yaml_file)
        os.chdir(repo_root_path)
        git_repo.git.commit("-a", "-m", f"Delete the deployment definition file")

        # 4. Run a deployment GitHub action but disallow deployment deletion
        run_github_action(
            repo_root_path,
            git_repo,
            main_branch_name,
            "push",
            is_deploy=True,
            allow_deployment_deletion=False,
        )
        printout("Validate ...")
        deployments = dr_client.fetch_deployments()
        local_git_deployment_id = deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        assert any(d.get("gitDeploymentId") == local_git_deployment_id for d in deployments)

        # 5. Run a deployment GitHub action for pull request with allowed deployment deletion
        printout("Run deployment GitHub action (pull request) with allowed deletion ...")
        run_github_action(
            repo_root_path,
            git_repo,
            main_branch_name,
            "pull_request",
            is_deploy=True,
            allow_deployment_deletion=True,
        )
        printout("Validate ...")
        deployments = dr_client.fetch_deployments()
        local_git_deployment_id = deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        assert any(d.get("gitDeploymentId") == local_git_deployment_id for d in deployments)

        # 6. Run a deployment GitHub action for push with allowed deployment deletion
        printout("Run deployment GitHub action (push) with allowed deletion ...")
        run_github_action(
            repo_root_path,
            git_repo,
            main_branch_name,
            "push",
            is_deploy=True,
            allow_deployment_deletion=True,
        )
        printout("Validate ...")
        deployments = dr_client.fetch_deployments()
        local_git_deployment_id = deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        assert all(d.get("gitDeploymentId") != local_git_deployment_id for d in deployments)
        printout("Done")

    @pytest.mark.parametrize("event_name", ["push", "pull_request"])
    @pytest.mark.usefixtures("cleanup", "set_model_dataset_for_testing")
    def test_e2e_deployment_model_challengers(
        self,
        dr_client,
        repo_root_path,
        git_repo,
        model_metadata_yaml_file,
        deployment_metadata,
        deployment_metadata_yaml_file,
        main_branch_name,
        event_name,
    ):
        """An end-to-end case to test challengers in a deployment."""

        # Enable challengers (although it is the default)
        printout("Enable challengers ...")
        self._enable_challenger(deployment_metadata, deployment_metadata_yaml_file, True)

        (
            the_deployment,
            latest_deployment_model_version_id,
            latest_model_version,
        ) = self._deploy_a_model_than_run_github_action_to_replace_or_challenge(
            dr_client,
            git_repo,
            repo_root_path,
            main_branch_name,
            deployment_metadata,
            model_metadata_yaml_file,
            event_name,
        )

        assert latest_deployment_model_version_id
        assert latest_model_version["id"]
        assert latest_deployment_model_version_id != latest_model_version["id"]

        challengers = dr_client.fetch_challengers(the_deployment["id"])
        if event_name == "push":
            assert len(challengers) == 2, challengers
            assert challengers[-1]["model"]["id"] == latest_model_version["id"]
        elif event_name == "pull_request":
            assert len(challengers) == 1, challengers
        else:
            assert False, f"Unsupported GitHub event name: {event_name}"
        printout("Done")

    @pytest.mark.parametrize("event_name", ["push", "pull_request"])
    @pytest.mark.usefixtures("cleanup", "skip_model_testing")
    def test_e2e_deployment_settings(
        self,
        dr_client,
        repo_root_path,
        git_repo,
        deployment_metadata,
        deployment_metadata_yaml_file,
        main_branch_name,
        event_name,
    ):
        """An end-to-end case to test changes in deployment settings."""

        # 1. Create a model just as a basic requirement (use GitHub action)
        printout(
            "Create a custom model as a preliminary requirement. "
            "Run custom model GitHub action (push event) ..."
        )
        run_github_action(repo_root_path, git_repo, main_branch_name, "push", is_deploy=False)

        # 2. Run a deployment GitHub action to create a deployment
        printout("Create a deployment. Run a deployment GitHub action (push event) ...")
        run_github_action(repo_root_path, git_repo, main_branch_name, "push", is_deploy=True)
        local_git_deployment_id = deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        deployment = dr_client.fetch_deployment_by_git_id(local_git_deployment_id)
        assert deployment is not None

        for check_func in [self._test_deployment_label, self._test_deployment_settings]:
            with check_func(
                dr_client,
                deployment,
                deployment_metadata,
                deployment_metadata_yaml_file,
                event_name,
            ):
                printout(f"Run deployment GitHub action ({event_name}")
                run_github_action(
                    repo_root_path, git_repo, main_branch_name, event_name, is_deploy=True
                )

    @staticmethod
    @contextlib.contextmanager
    def _test_deployment_label(
        dr_client, deployment, deployment_metadata, deployment_metadata_yaml_file, event_name
    ):
        printout("Change deployment name")
        old_name = deployment["label"]
        new_name = f"{old_name} - NEW"
        with temporarily_replace_schema_value(
            deployment_metadata_yaml_file,
            DeploymentSchema.SETTINGS_SECTION_KEY,
            DeploymentSchema.LABEL_KEY,
            new_value=new_name,
        ):
            yield

        deployment = dr_client.fetch_deployment_by_git_id(
            deployment_metadata[DeploymentSchema.DEPLOYMENT_ID_KEY]
        )
        if event_name == "push":
            assert deployment["label"] == new_name
        elif event_name == "pull_request":
            assert deployment["label"] == old_name
        else:
            assert False, f"Unsupported GitHub event name: {event_name}"

    @staticmethod
    @contextlib.contextmanager
    def _test_deployment_settings(
        dr_client,
        deployment,
        deployment_metadata,
        deployment_metadata_yaml_file,
        event_name,
    ):
        deployment_info = DeploymentInfo(deployment_metadata_yaml_file, deployment_metadata)
        deployment_settings = dr_client.fetch_deployment_settings(deployment["id"], deployment_info)

        new_value = not deployment_settings["targetDrift"]["enabled"]
        deployment_info.set_settings_value(
            DeploymentSchema.ENABLE_TARGET_DRIFT_KEY, value=new_value
        )

        new_value = not deployment_settings["featureDrift"]["enabled"]
        # TODO: Remove the next line when a support for learning data assignment is implemented
        new_value = False
        deployment_info.set_settings_value(
            DeploymentSchema.ENABLE_FEATURE_DRIFT_KEY, value=new_value
        )

        new_value = not deployment_settings["segmentAnalysis"]["enabled"]
        deployment_info.set_settings_value(
            DeploymentSchema.SEGMENT_ANALYSIS_KEY,
            DeploymentSchema.ENABLE_SEGMENT_ANALYSIS_KEY,
            value=new_value,
        )

        new_value = not deployment_settings["challengerModels"]["enabled"]
        deployment_info.set_settings_value(
            DeploymentSchema.ENABLE_CHALLENGER_MODELS_KEY, value=new_value
        )
        deployment_info.set_settings_value(
            DeploymentSchema.ENABLE_PREDICTIONS_COLLECTION_KEY, value=new_value
        )

        with temporarily_replace_schema(deployment_metadata_yaml_file, deployment_info.metadata):
            yield

        new_deployment_settings = dr_client.fetch_deployment_settings(
            deployment["id"], deployment_info
        )
        if event_name == "push":
            expected_target_drift = deployment_info.get_settings_value(
                DeploymentSchema.ENABLE_TARGET_DRIFT_KEY
            )
            expected_feature_drift = deployment_info.get_settings_value(
                DeploymentSchema.ENABLE_FEATURE_DRIFT_KEY
            )
            expected_segment_analysis = deployment_info.get_settings_value(
                DeploymentSchema.SEGMENT_ANALYSIS_KEY, DeploymentSchema.ENABLE_SEGMENT_ANALYSIS_KEY
            )
            expected_challenger_models = deployment_info.get_settings_value(
                DeploymentSchema.ENABLE_CHALLENGER_MODELS_KEY
            )
            expected_predictions_data_collection = deployment_info.get_settings_value(
                DeploymentSchema.ENABLE_PREDICTIONS_COLLECTION_KEY
            )
        elif event_name == "pull_request":
            expected_target_drift = deployment_settings["targetDrift"]["enabled"]
            expected_feature_drift = deployment_settings["featureDrift"]["enabled"]
            expected_segment_analysis = new_deployment_settings["segmentAnalysis"]["enabled"]
            expected_challenger_models = deployment_settings["challengerModels"]["enabled"]
            expected_predictions_data_collection = deployment_settings["predictionsDataCollection"][
                "enabled"
            ]
        else:
            assert False, f"Unsupported GitHub event name: {event_name}"

        assert expected_target_drift == new_deployment_settings["targetDrift"]["enabled"]
        assert expected_feature_drift == new_deployment_settings["featureDrift"]["enabled"]
        assert expected_segment_analysis == new_deployment_settings["segmentAnalysis"]["enabled"]
        assert expected_challenger_models == new_deployment_settings["challengerModels"]["enabled"]
        assert (
            expected_predictions_data_collection
            == new_deployment_settings["predictionsDataCollection"]["enabled"]
        )
