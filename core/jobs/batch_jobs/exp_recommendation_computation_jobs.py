# coding: utf-8
#
# Copyright 2021 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Jobs that are run by CRON scheduler."""

from __future__ import annotations

from core.domain import recommendations_services
from core.jobs import base_jobs
from core.jobs.io import ndb_io
from core.jobs.types import job_run_result
from core.platform import models

import apache_beam as beam

from typing import Dict, Iterable, List, Tuple, Union, cast

MYPY = False
if MYPY: # pragma: no cover
    from mypy_imports import datastore_services
    from mypy_imports import exp_models
    from mypy_imports import recommendations_models

(exp_models, recommendations_models) = models.Registry.import_models(
    [models.NAMES.exploration, models.NAMES.recommendations])

datastore_services = models.Registry.import_datastore_services()

MAX_RECOMMENDATIONS = 10
# Note: There is a threshold so that bad recommendations will be
# discarded even if an exploration has few similar explorations.
SIMILARITY_SCORE_THRESHOLD = 3.0


class ComputeExplorationRecommendationsJob(base_jobs.JobBase):
    """Job that indexes the explorations in Elastic Search."""

    def run(self) -> beam.PCollection[job_run_result.JobRunResult]:
        """Returns a PCollection of 'SUCCESS' or 'FAILURE' results from
        the Elastic Search.

        Returns:
            PCollection. A PCollection of 'SUCCESS' or 'FAILURE' results from
            the Elastic Search.
        """

        exp_summary_models = (
            self.pipeline
            | 'Get all non-deleted models' >> (
                ndb_io.GetModels(exp_models.ExpSummaryModel.get_all()))
        )

        exp_summary_iter = beam.pvalue.AsIter(exp_summary_models)

        exp_recommendations_models = (
            exp_summary_models
            | 'Compute similarity' >> beam.ParDo(
                ComputeSimilarity(), exp_summary_iter)
            | 'Group similarities per exploration ID' >> beam.GroupByKey()
            | 'Sort and slice similarities' >> beam.MapTuple(
                lambda exp_id, similarities: (
                    exp_id, self._sort_and_slice_similarities(similarities)))
            | 'Create recommendation models' >> beam.MapTuple(
                self._create_recommendation)
        )

        unused_put_result = (
            exp_recommendations_models
            | 'Put models into the datastore' >> ndb_io.PutModels()
        )

        return (
            exp_recommendations_models
            | 'Count all new models' >> beam.combiners.Count.Globally()
            | 'Only create result for new models when > 0' >> (
                beam.Filter(lambda x: x > 0))
            | 'Create result for new models' >> beam.Map(
                lambda x: job_run_result.JobRunResult(
                    stdout='SUCCESS %s' % x))
        )

    @staticmethod
    def _sort_and_slice_similarities(
            similarities: Iterable[Dict[str, Union[str, float]]]
    ) -> List[str]:
        """Sorts similarities of explorations and slices them to
        a maximum length.

        Args:
            similarities:iterable(). Iterable of dictionaries. The structure of
                the dictionaries is:
                    exp_id: str. The ID of the similar exploration.
                    similarity_score: float. The similarity score for
                        the exploration.

        Returns:
            list(str). List of exploration IDs, sorted by the similarity.
        """
        sorted_similarities = sorted(
            similarities, reverse=True, key=lambda x: x['similarity_score'])
        return [
                str(item['exp_id']) for item in sorted_similarities
            ][:MAX_RECOMMENDATIONS]

    @staticmethod
    def _create_recommendation(
            exp_id: str, recommended_exp_ids: Iterable[str]
    ) -> recommendations_models.ExplorationRecommendationsModel:
        """Creates exploration recommendation model.

        Args:
            exp_id: str. The exploration ID for which the recommendation is
                created.
            recommended_exp_ids: list(str). The list of recommended
                exploration IDs.

        Returns:
            ExplorationRecommendationsModel. The created model.
        """
        with datastore_services.get_ndb_context():
            exp_recommendation_model = (
                recommendations_models.ExplorationRecommendationsModel(
                    id=exp_id, recommended_exploration_ids=recommended_exp_ids))
        exp_recommendation_model.update_timestamps()
        return exp_recommendation_model


class ComputeSimilarity(beam.DoFn):  # type: ignore[misc]
    """DoFn to compute similarities between exploration."""

    def process(
        self,
        ref_exp_summary_model: datastore_services.Model,
        compared_exp_summary_models: Iterable[datastore_services.Model]
    ) -> Iterable[Tuple[str, Dict[str, Union[str, float]]]]:
        """Compute similarities between exploraitons.

        Args:
            ref_exp_summary_model: ExpSummaryModel. Reference exploration
                summary. We are trying to find explorations similar to this
                reference summary.
            compared_exp_summary_models: list(ExpSummaryModel). List of other
                explorations summaries against which we compare the reference
                summary.

        Yields:
            (str, dict(str, str|float)). Tuple, the first element is
            the exploration ID of the reference exploration summary.
            The second is a dictionary. The structure of the dictionary is:
                exp_id: str. The ID of the similar exploration.
                similarity_score: float. The similarity score for
                    the exploration.
        """
        ref_exp_summary_model = cast(
            exp_models.ExpSummaryModel, ref_exp_summary_model)
        with datastore_services.get_ndb_context():
            for compared_exp_summary_model in compared_exp_summary_models:
                compared_exp_summary_model = cast(
                    exp_models.ExpSummaryModel,
                    compared_exp_summary_model
                )
                if compared_exp_summary_model.id == ref_exp_summary_model.id:
                    continue
                similarity_score = recommendations_services.get_item_similarity( # type: ignore[no-untyped-call]
                    ref_exp_summary_model, compared_exp_summary_model
                )
                if similarity_score >= SIMILARITY_SCORE_THRESHOLD:
                    yield (
                        ref_exp_summary_model.id, {
                            'similarity_score': similarity_score,
                            'exp_id': compared_exp_summary_model.id
                        }
                    )
