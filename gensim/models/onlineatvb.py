#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2011 Radim Rehurek <radimrehurek@seznam.cz>
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html

"""
Author-topic model.
"""

import pdb
from pdb import set_trace as st

import logging
import numpy
import numbers

from gensim import utils, matutils
from gensim.models.ldamodel import dirichlet_expectation, get_random_state
from gensim.models import LdaModel
from gensim.models import AtVb
from gensim.models.hdpmodel import log_normalize  # For efficient normalization of variational parameters.
from six.moves import xrange
from scipy.special import gammaln

from pprint import pprint

# log(sum(exp(x))) that tries to avoid overflow
try:
    # try importing from here if older scipy is installed
    from scipy.maxentropy import logsumexp
except ImportError:
    # maxentropy has been removed in recent releases, logsumexp now in misc
    from scipy.misc import logsumexp

logger = logging.getLogger(__name__)


class OnlineAtVb(AtVb):
    """
    Train the author-topic model using online variational Bayes.
    """
    # TODO: inherit interfaces.TransformationABC.

    def __init__(self, corpus=None, num_topics=100, id2word=None, id2author=None,
            author2doc=None, doc2author=None, threshold=0.001, minimum_probability=0.01,
            iterations=10, passes=1, alpha=None, eta=None, decay=0.5, offset=1.0,
            eval_every=1, random_state=None):

        self.id2word = id2word
        if corpus is None and self.id2word is None:
            raise ValueError('at least one of corpus/id2word must be specified, to establish input space dimensionality')

        # NOTE: this stuff is confusing to me (from LDA code). Why would id2word not be none, but have length 0?
        if self.id2word is None:
            logger.warning("no word id mapping provided; initializing from corpus, assuming identity")
            self.id2word = utils.dict_from_corpus(corpus)
            self.num_terms = len(self.id2word)
        elif len(self.id2word) > 0:
            self.num_terms = 1 + max(self.id2word.keys())
        else:
            self.num_terms = 0

        if self.num_terms == 0:
            raise ValueError("cannot compute LDA over an empty collection (no terms)")

        logger.info('Vocabulary consists of %d words.', self.num_terms)

        if doc2author is None and author2doc is None:
            raise ValueError('at least one of author2doc/doc2author must be specified, to establish input space dimensionality')

        # TODO: consider whether there is a more elegant way of doing this (more importantly, a more efficient way).
        # If either doc2author or author2doc is missing, construct them from the other.
        if doc2author is None:
            # Make a mapping from document IDs to author IDs.
            doc2author = {}
            for d, _ in enumerate(corpus):
                author_ids = []
                for a, a_doc_ids in author2doc.items():
                    if d in a_doc_ids:
                        author_ids.append(a)
                doc2author[d] = author_ids
        elif author2doc is None:
            # Make a mapping from author IDs to document IDs.

            # First get a set of all authors.
            authors_ids = set()
            for d, a_doc_ids in doc2author.items():
                for a in a_doc_ids:
                    authors_ids.add(a)

            # Now construct the dictionary.
            author2doc = {}
            for a in range(len(authors_ids)):
                author2doc[a] = []
                for d, a_ids in doc2author.items():
                    if a in a_ids:
                        author2doc[a].append(d)

        self.author2doc = author2doc
        self.doc2author = doc2author

        self.num_authors = len(self.author2doc)
        logger.info('Number of authors: %d.', self.num_authors)

        self.id2author = id2author
        if self.id2author is None:
            logger.warning("no author id mapping provided; initializing from corpus, assuming identity")
            author_integer_ids = [str(i) for i in range(len(author2doc))]
            self.id2author = dict(zip(range(len(author2doc)), author_integer_ids))

        # Make the reverse mapping, from author names to author IDs.
        self.author2id = dict(zip(self.id2author.values(), self.id2author.keys()))

        # NOTE: I don't think this necessarily is a good way to initialize the topics.
        self.alpha = numpy.asarray([1.0 / self.num_topics for i in xrange(self.num_topics)])
        self.eta = numpy.asarray([1.0 / self.num_terms for i in xrange(self.num_terms)])

        self.corpus = corpus
        self.iterations = iterations
        self.passes = passes
        self.num_topics = num_topics
        self.threshold = threshold
        self.minimum_probability = minimum_probability 
        self.decay = decay
        self.offset = offset
        self.num_docs = len(corpus)
        self.num_authors = len(author2doc)
        self.eval_every = eval_every
        self.random_state = random_state

        self.random_state = get_random_state(random_state)

        if corpus is not None:
            self.inference(corpus)

    def rho(self, t):
        return pow(self.offset + t, -self.decay)

    def inference(self, corpus=None):
        if corpus is None:
            # TODO: I can't remember why I used "copy()" here.
            corpus = self.corpus.copy()

        self.num_docs = len(corpus)  # TODO: this needs to be different if the algorithm is truly online.

        logger.info('Starting inference. Training on %d documents.', len(corpus))

        # Initial values of gamma and lambda.
        # NOTE: parameters of gamma distribution same as in `ldamodel`.
        init_gamma = self.random_state.gamma(100., 1. / 100.,
                (self.num_authors, self.num_topics))
        init_lambda = self.random_state.gamma(100., 1. / 100.,
                (self.num_topics, self.num_terms))

        # TODO: consider making phi sparse. Each document does not contain all terms.
        var_phi = numpy.zeros((self.num_terms, self.num_topics))

        var_gamma = init_gamma.copy()
        var_lambda = init_lambda.copy()
        tilde_gamma = init_gamma.copy()
        tilde_lambda = init_lambda.copy()

        # Initialize dirichlet expectations.
        Elogtheta = dirichlet_expectation(var_gamma)
        Elogbeta = dirichlet_expectation(var_lambda)
        expElogbeta = numpy.exp(Elogbeta)

        # Evaluate bound.
        word_bound = self.word_bound(Elogtheta, Elogbeta)
        theta_bound = self.theta_bound(Elogtheta, var_gamma)
        beta_bound = self.beta_bound(Elogbeta, var_lambda)
        bound = word_bound + theta_bound + beta_bound
        #likelihood = self.log_word_prob(var_gamma, var_lambda)
        logger.info('Total bound: %.3e. Word bound: %.3e. theta bound: %.3e. beta bound: %.3e.', bound, word_bound, theta_bound, beta_bound)
        t = 0
        for _pass in xrange(self.passes):
            converged = 0  # Number of documents converged for current pass over corpus.
            prev_bound = bound
            for d, doc in enumerate(corpus):
                ids = numpy.array([id for id, _ in doc])  # Word IDs in doc.
                cts = numpy.array([cnt for _, cnt in doc])  # Word counts.
                authors_d = self.doc2author[d]  # List of author IDs for document d.

                # Initialize mu.
                # mu is 1/|A_d| if a is in A_d, zero otherwise.
                # TODO: consider doing random initialization instead.
                # TODO: consider making mu a sparse matrix instead of a dictionary.
                var_mu = dict()
                for v in ids:
                    for a in authors_d:
                        var_mu[(v, a)] = 1 / len(authors_d)

                for iteration in xrange(self.iterations):
                    #logger.info('iteration %i', iteration)

                    lastgamma = tilde_gamma.copy()
                    lastlambda = tilde_lambda.copy()

                    # Update phi.
                    for v in ids:
                        for k in xrange(self.num_topics):
                            # Average Elogtheta over authors a in document d.
                            avgElogtheta = 0.0
                            for a in authors_d:
                                avgElogtheta += var_mu[(v, a)] * Elogtheta[a, k]
                            expavgElogtheta = numpy.exp(avgElogtheta)

                            # Compute phi.
                            # TODO: avoid computing phi if possible.
                            var_phi[v, k] = expavgElogtheta * expElogbeta[k, v]

                        # Normalize phi over k.
                        var_phi[v, :] = var_phi[v, :] / var_phi[v, :].sum()

                    # Update mu.
                    for v in ids:
                        # Prior probability of observing author a in document d is one
                        # over the number of authors in document d.
                        mu_sum = 0.0
                        for a in authors_d:
                            # Average Elogtheta over topics k.
                            avgElogtheta = 0.0
                            for k in xrange(self.num_topics):
                                avgElogtheta += var_phi[v, k] * Elogtheta[a, k]
                            expavgElogtheta = numpy.exp(avgElogtheta)

                            # Compute mu over a.
                            # TODO: avoid computing mu if possible.
                            var_mu[(v, a)] = expavgElogtheta
                            mu_sum += var_mu[(v, a)]

                        # Normalize mu.
                        mu_norm_const = 1.0 / mu_sum
                        for a in authors_d:
                            var_mu[(v, a)] *= mu_norm_const

                    # Update gamma.
                    for a in authors_d:
                        for k in xrange(self.num_topics):
                            tilde_gamma[a, k] = 0.0
                            for vi, v in enumerate(ids):
                                tilde_gamma[a, k] += cts[vi] * var_mu[(v, a)] * var_phi[v, k]
                            tilde_gamma[a, k] *= len(self.author2doc[a])
                            tilde_gamma[a, k] += self.alpha

                    # Update lambda.
                    #tilde_lambda = self.eta + self.num_docs * cts * var_phi[ids, :].T
                    for k in xrange(self.num_topics):
                        for vi, v in enumerate(ids):
                            cnt = dict(doc).get(v, 0)
                            var_lambda[k, v] = self.eta + self.num_docs * cnt * var_phi[v, k]

                    # Check for convergence.
                    # Criterion is mean change in "local" gamma and lambda.
                    # TODO: consider using separate thresholds for lambda and gamma.
                    if iteration > 0:
                        meanchange_gamma = numpy.mean(abs(tilde_gamma - lastgamma))
                        meanchange_lambda = numpy.mean(abs(tilde_lambda - lastlambda))
                        # logger.info('Mean change in gamma: %.3e', meanchange_gamma)
                        # logger.info('Mean change in lambda: %.3e', meanchange_lambda)
                        if meanchange_gamma < self.threshold and meanchange_lambda < self.threshold:
                            # logger.info('Converged after %d iterations.', iteration)
                            converged += 1
                            break
                # End of iterations loop.

                # TODO: I don't need to update the entire gamma, as I only updated a few rows of it,
                # corresponding to the authors in the document. The same goes for Elogtheta.

                # Update gamma and lambda.
                # Interpolation between document d's "local" gamma (tilde_gamma),
                # and "global" gamma (var_gamma). Same goes for lambda.
                # TODO: I may need to be smarter about computing rho. In ldamodel,
                # it's: pow(offset + pass_ + (self.num_updates / chunksize), -decay).
                rhot = self.rho(t)
                t += 1
                var_gamma = (1 - rhot) * var_gamma + rhot * tilde_gamma
                # Note that we only changed the elements in lambda corresponding to 
                # the words in document d, hence the [:, ids] indexing.
                var_lambda[:, ids] = (1 - rhot) * var_lambda[:, ids] + rhot * tilde_lambda[:, ids]

                # Update Elogtheta and Elogbeta, since gamma and lambda have been updated.
                Elogtheta = dirichlet_expectation(var_gamma)
                Elogbeta = dirichlet_expectation(var_lambda)
                expElogbeta = numpy.exp(Elogbeta)

                # Print topics:
                # self.var_lambda = var_lambda
                # pprint(self.show_topics())

            # End of corpus loop.

            # Evaluate bound.
            if _pass % self.eval_every == 0:
                word_bound = self.word_bound(Elogtheta, Elogbeta)
                theta_bound = self.theta_bound(Elogtheta, var_gamma)
                beta_bound = self.beta_bound(Elogbeta, var_lambda)
                bound = word_bound + theta_bound + beta_bound
                #likelihood = self.log_word_prob(var_gamma, var_lambda)
                logger.info('Total bound: %.3e. Word bound: %.3e. theta bound: %.3e. beta bound: %.3e.', bound, word_bound, theta_bound, beta_bound)

            logger.info('Converged documents: %d/%d', converged, self.num_docs)

            # TODO: consider whether to include somthing like this:
            #if numpy.abs(bound - prev_bound) / abs(prev_bound) < self.bound_threshold:
            #    break
        # End of pass over corpus loop.


        self.var_lambda = var_lambda
        self.var_gamma = var_gamma

        return var_gamma, var_lambda

    def word_bound(self, Elogtheta, Elogbeta, doc_ids=None):
        """
        Note that this is not strictly speaking a likelihood.

        Compute the expectation of the log conditional likelihood of the data,

            E_q[log p(w_d | theta, beta, A_d)],

        where p(w_d | theta, beta, A_d) is the log conditional likelihood of the data.
        """

        # TODO: allow for evaluating test corpus. This will require inferring on unseen documents.

        if doc_ids is None:
            docs = self.corpus
        else:
            docs = [self.corpus[d] for d in doc_ids]

        bound= 0.0
        for d, doc in enumerate(docs):
            authors_d = self.doc2author[d]
            ids = numpy.array([id for id, _ in doc])  # Word IDs in doc.
            cts = numpy.array([cnt for _, cnt in doc])  # Word counts.
            bound_d = 0.0
            for vi, v in enumerate(ids):
                bound_v = 0.0
                for k in xrange(self.num_topics):
                    for a in authors_d:
                        bound_v += numpy.exp(Elogtheta[a, k] + Elogbeta[k, v])
                bound_d += cts[vi] * numpy.log(bound_v)
            bound += numpy.log(1.0 / len(authors_d)) + bound_d

        # For per-word likelihood, do:
        # likelihood *= 1 /sum(len(doc) for doc in docs)

        # TODO: can I do something along the lines of (as in ldamodel):
        # likelihood += numpy.sum(cnt * logsumexp(Elogthetad + Elogbeta[:, id]) for id, cnt in doc)

        return bound

    def theta_bound(self, Elogtheta, var_gamma, doc_ids=None):
        """
        """

        if doc_ids is None:
            docs = self.corpus
        else:
            docs = [self.corpus[d] for d in doc_ids]

        bound = 0.0
        for a in xrange(self.num_authors):
            var_gamma_a = var_gamma[a, :]
            Elogtheta_a = Elogtheta[a, :]
            # E[log p(theta | alpha) - log q(theta | gamma)]; assumes alpha is a vector
            bound += numpy.sum((self.alpha - var_gamma_a) * Elogtheta_a)
            bound += numpy.sum(gammaln(var_gamma_a) - gammaln(self.alpha))
            bound += gammaln(numpy.sum(self.alpha)) - gammaln(numpy.sum(var_gamma_a))

        return bound

    def beta_bound(self, Elogbeta, var_lambda, doc_ids=None):
        bound = 0.0
        bound += numpy.sum((self.eta - var_lambda) * Elogbeta)
        bound += numpy.sum(gammaln(var_lambda) - gammaln(self.eta))
        bound += numpy.sum(gammaln(numpy.sum(self.eta)) - gammaln(numpy.sum(var_lambda, 1)))

        return bound

    def log_word_prob(self, var_gamma, var_lambda, doc_ids=None):
        """
        Compute the liklihood of the corpus under the model, by first 
        computing the conditional probabilities of the words in a
        document d,

            p(w_d | theta, beta, A_d),

        summing over all documents, and dividing by the number of documents.
        """

        norm_gamma = var_gamma.copy()
        norm_lambda = var_lambda.copy()
        for a in xrange(self.num_authors):
            norm_gamma[a, :] = var_gamma[a, :] / var_gamma.sum(axis=1)[a]
        for k in xrange(self.num_topics):
            norm_lambda[k, :] = var_lambda[k, :] / var_lambda.sum(axis=1)[k]

        if doc_ids is None:
            docs = self.corpus
        else:
            docs = [self.corpus[d] for d in doc_ids]

        log_word_prob = 0.0
        for d, doc in enumerate(docs):
            ids = numpy.array([id for id, _ in doc])  # Word IDs in doc.
            cts = numpy.array([cnt for _, cnt in doc])  # Word counts.
            authors_d = self.doc2author[d]
            log_word_prob_d = 0.0
            for vi, v in enumerate(ids):
                log_word_prob_v = 0.0
                for k in xrange(self.num_topics):
                    for a in authors_d:
                        log_word_prob_v += norm_gamma[a, k] * norm_lambda[k, v]
                log_word_prob_d += cts[vi] * numpy.log(log_word_prob_v)
            log_word_prob += numpy.log(1.0 / len(authors_d)) + log_word_prob_d
            #authors_idxs = [self.authorid2idx[aid] for aid in authors_d]
            #likelihood += author_prior_prob * numpy.sum(cnt * numpy.log(numpy.sum(numpy.exp(logsumexp(Elogtheta[a, :] + Elogbeta[:, id])) for a in authors_idxs)) for id, cnt in doc)

        return log_word_prob

    # Overriding LdaModel.get_topic_terms.
    def get_topic_terms(self, topicid, topn=10):
        """
        Return a list of `(word_id, probability)` 2-tuples for the most
        probable words in topic `topicid`.
        Only return 2-tuples for the topn most probable words (ignore the rest).
        """
        topic = self.var_lambda[topicid, :]
        topic = topic / topic.sum()  # normalize to probability distribution
        bestn = matutils.argsort(topic, topn, reverse=True)
        return [(id, topic[id]) for id in bestn]

    def get_author_topics(self, author_id, minimum_probability=None):
        """
        Return topic distribution the given author, as a list of
        (topic_id, topic_probability) 2-tuples.
        Ignore topics with very low probability (below `minimum_probability`).
        """
        if minimum_probability is None:
            minimum_probability = self.minimum_probability
        minimum_probability = max(minimum_probability, 1e-8)  # never allow zero values in sparse output

        topic_dist = self.var_gamma[author_id, :] / sum(self.var_gamma[author_id, :])

        author_topics = [(topicid, topicvalue) for topicid, topicvalue in enumerate(topic_dist)
                if topicvalue >= minimum_probability]

        # author_name = self.id2author[author_id]

        return author_topics







